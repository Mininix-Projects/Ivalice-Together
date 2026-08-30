from __future__ import annotations

from dataclasses import dataclass
import struct
import threading
import time

import coop_runtime as coop
import native_ui_tick_mux as ui_tick_mux


UI_MANAGER_TICK_CALL = ui_tick_mux.UI_MANAGER_TICK_CALL
UI_MANAGER_TICK = ui_tick_mux.UI_MANAGER_TICK
UI_MANAGER_TICK_RETURN = ui_tick_mux.UI_MANAGER_TICK_RETURN
ORIGINAL_TICK_CALL = ui_tick_mux.ORIGINAL_TICK_CALL
TARGET_CIRCLE_RETARGET = 0x2917E0
TARGET_CIRCLE_PALETTE_COMMAND = 0x271650
TARGET_UI_MANAGER_GLOBAL = 0x209BE80
TARGET_CIRCLE_ALLOCATOR_OFFSET = 0x10FC0

# 0x2917E0 reads the native palette twice on its existing-node path: once for
# the packed render command and once for node+0xD7C.  Its create path reads the
# same table before constructing the new node.  Five-byte CALL gates replace
# only those loads and return the original table value unless the resident
# dispatcher has armed one authenticated CREATE/RETARGET transaction.
TARGET_PALETTE_COMMAND_LOAD_SITE = 0x291858
TARGET_PALETTE_FIELD_LOAD_SITE = 0x291893
TARGET_PALETTE_CREATE_LOAD_SITE = 0x291ACE
TARGET_PALETTE_COMMAND_LOAD_ORIGINAL = b"\x4f\x0f\xbe\x0c\x3c"
TARGET_PALETTE_FIELD_LOAD_ORIGINAL = b"\x43\x0f\xbe\x04\x3c"
TARGET_PALETTE_CREATE_LOAD_ORIGINAL = b"\x47\x0f\xbe\x04\x3c"

REGION_MAGIC = b"FF12NCR7"
REGION_SIZE = 0x1000
SERVICE_OFFSET = 0x200
CODE_OFFSET = ui_tick_mux.TARGET_CIRCLE_HANDLER_OFFSET
STATE_OFFSET = 0xE70
STATE_SIZE = REGION_SIZE - STATE_OFFSET

TARGET_PALETTE_COMMAND_HOOK_OFFSET = 0x20
TARGET_PALETTE_FIELD_HOOK_OFFSET = 0x35
TARGET_PALETTE_CREATE_HOOK_OFFSET = 0x45
# Relative to SERVICE_OFFSET.  The relaxed callback ends below this immutable
# tail, leaving the service header entirely available for ABI-correct wrappers.
TARGET_PALETTE_SELECTOR_OFFSET = 0xBA8

TARGET_OPERATION_CREATE = 1
TARGET_OPERATION_RETARGET = 2
TARGET_OPERATION_RETIRE = 3
TARGET_OPERATION_RECONCILE = 4
TARGET_OPERATION_OFFSET = 0x04
TARGET_NODE_OFFSET = 0x08
TARGET_HANDLE_OFFSET = 0x10
TARGET_VARIANT_OFFSET = 0x14
TARGET_OBSERVED_OFFSET = 0x18
TARGET_SEQUENCE_OFFSET = 0x1C
TARGET_OWNER_OFFSET = 0x20
# CREATE uses this dword as a durable "native factory entered" bit.  Every
# owned operation uses the same offset for the resident ownership token.
TARGET_FACTORY_ENTERED_OFFSET = 0x24
TARGET_AUTH_TOKEN_OFFSET = TARGET_FACTORY_ENTERED_OFFSET
# CREATE publishes its effective heap identity here.  Owned operations carry
# the manager/allocator identity which the game-thread callback must match.
TARGET_RESULT_MANAGER_OFFSET = 0x28
TARGET_RESULT_ALLOCATOR_OFFSET = 0x30
TARGET_RESULT_RESERVED_OFFSET = 0x38
TARGET_EXPECTED_MANAGER_OFFSET = TARGET_RESULT_MANAGER_OFFSET
TARGET_EXPECTED_ALLOCATOR_OFFSET = TARGET_RESULT_ALLOCATOR_OFFSET
TARGET_EXPECTED_TARGET_OFFSET = TARGET_RESULT_RESERVED_OFFSET
TARGET_EXPECTED_PALETTE_OFFSET = 0x3C
TARGET_ACK_SEQUENCE_OFFSET = 0x40
TARGET_OBSERVED_PALETTE_OFFSET = 0x44
TARGET_DESIRED_PALETTE_OFFSET = 0x48
TARGET_REQUEST_RESERVED_OFFSET = 0x4C
TARGET_PALETTE_OVERRIDE_COOKIE = 0xC17C1E06
TARGET_STATE_MAGIC_OFFSET = 0x50
TARGET_STATE_MAGIC = b"TCRMBX6!"

# The request prefix above is transient.  Ownership lives in a separate,
# process-resident registry that ``arm`` never overwrites.  Fixed owner slots
# make a palette, target or list position categorically insufficient to claim a
# node after a helper restart.
TARGET_OWNERSHIP_MAGIC_OFFSET = 0x80
TARGET_OWNERSHIP_MAGIC = b"TCOWNR6!"
TARGET_OWNERSHIP_VERSION = 4
TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET = TARGET_OWNERSHIP_MAGIC_OFFSET + 0x10
TARGET_OWNERSHIP_RECORDS_OFFSET = 0x100
TARGET_OWNERSHIP_RECORD_SIZE = 0x30
TARGET_OWNERSHIP_RECORD_COUNT = 3
TARGET_OWNERSHIP_RECORDS_END = (
    TARGET_OWNERSHIP_RECORDS_OFFSET
    + TARGET_OWNERSHIP_RECORD_SIZE * TARGET_OWNERSHIP_RECORD_COUNT
)

P1_TARGET_OWNER_TAG = int.from_bytes(b"TCP1", "little")
P2_TARGET_OWNER_TAG = int.from_bytes(b"TCP2", "little")
P3_TARGET_OWNER_TAG = int.from_bytes(b"TCP3", "little")
TARGET_OWNER_TAGS = (
    P1_TARGET_OWNER_TAG,
    P2_TARGET_OWNER_TAG,
    P3_TARGET_OWNER_TAG,
)

_OWNERSHIP_CHECKSUM_SALT = 0x54434F57  # "WOCT" / target-circle ownership.


@dataclass(frozen=True)
class TargetCircleOwnership:
    owner_tag: int
    token: int
    manager: int
    allocator: int
    node: int
    target: int
    palette: int


@dataclass(frozen=True)
class TargetCircleCreateJournal:
    status: int
    sequence: int
    owner_tag: int
    factory_entered: bool
    manager: int
    allocator: int
    node: int
    target: int
    observed: int
    palette: int


def _owner_slot(owner_tag: int) -> int:
    owner_tag = int(owner_tag) & 0xFFFFFFFF
    try:
        return TARGET_OWNER_TAGS.index(owner_tag)
    except ValueError as error:
        raise ValueError("target-circle owner must identify P1, P2 or P3") from error


def _ownership_checksum(ownership: TargetCircleOwnership) -> int:
    manager = int(ownership.manager) & 0xFFFFFFFFFFFFFFFF
    allocator = int(ownership.allocator) & 0xFFFFFFFFFFFFFFFF
    node = int(ownership.node) & 0xFFFFFFFFFFFFFFFF
    return (
        int(ownership.owner_tag)
        ^ int(ownership.token)
        ^ (manager & 0xFFFFFFFF)
        ^ (manager >> 32)
        ^ (allocator & 0xFFFFFFFF)
        ^ (allocator >> 32)
        ^ (node & 0xFFFFFFFF)
        ^ (node >> 32)
        ^ int(ownership.target)
        ^ int(ownership.palette)
        ^ _OWNERSHIP_CHECKSUM_SALT
    ) & 0xFFFFFFFF


def _pack_ownership_record(ownership: TargetCircleOwnership) -> bytes:
    _owner_slot(ownership.owner_tag)
    if (
        not ownership.token
        or not ownership.manager
        or not ownership.allocator
        or not ownership.node
    ):
        raise ValueError(
            "target-circle ownership requires token, manager, allocator and node"
        )
    if not ownership.target:
        raise ValueError("target-circle ownership requires a target")
    if not 0 <= int(ownership.palette) <= 6:
        raise ValueError("target-circle ownership palette is invalid")
    checksum = _ownership_checksum(ownership)
    record = bytearray(TARGET_OWNERSHIP_RECORD_SIZE)
    struct.pack_into(
        "<IIQQQIIII",
        record,
        0,
        int(ownership.owner_tag) & 0xFFFFFFFF,
        int(ownership.token) & 0xFFFFFFFF,
        int(ownership.manager) & 0xFFFFFFFFFFFFFFFF,
        int(ownership.allocator) & 0xFFFFFFFFFFFFFFFF,
        int(ownership.node) & 0xFFFFFFFFFFFFFFFF,
        int(ownership.target) & 0xFFFFFFFF,
        int(ownership.palette) & 0xFFFFFFFF,
        checksum,
        checksum ^ 0xFFFFFFFF,
    )
    return bytes(record)


def _unpack_ownership_record(
    image: bytes,
    slot: int,
) -> TargetCircleOwnership | None:
    if not 0 <= int(slot) < TARGET_OWNERSHIP_RECORD_COUNT:
        raise ValueError("target-circle ownership slot is invalid")
    offset = TARGET_OWNERSHIP_RECORDS_OFFSET + int(slot) * TARGET_OWNERSHIP_RECORD_SIZE
    record = bytes(image[offset : offset + TARGET_OWNERSHIP_RECORD_SIZE])
    if len(record) != TARGET_OWNERSHIP_RECORD_SIZE:
        raise ValueError("target-circle ownership record is truncated")
    if record == b"\x00" * TARGET_OWNERSHIP_RECORD_SIZE:
        return None
    (
        owner_tag,
        token,
        manager,
        allocator,
        node,
        target,
        palette,
        checksum,
        complement,
    ) = struct.unpack_from("<IIQQQIIII", record)
    ownership = TargetCircleOwnership(
        owner_tag=owner_tag,
        token=token,
        manager=manager,
        allocator=allocator,
        node=node,
        target=target,
        palette=palette,
    )
    if owner_tag != TARGET_OWNER_TAGS[int(slot)]:
        raise ValueError("target-circle ownership tag is in the wrong slot")
    expected = _ownership_checksum(ownership)
    if (
        not token
        or not manager
        or not allocator
        or not node
        or not target
        or not 0 <= palette <= 6
        or checksum != expected
        or complement != (expected ^ 0xFFFFFFFF)
    ):
        raise ValueError("target-circle ownership record is invalid")
    return ownership


def _unpack_create_journal(image: bytes) -> TargetCircleCreateJournal | None:
    """Return one unconsumed terminal CREATE transaction, if present."""

    if len(image) != STATE_SIZE:
        raise ValueError("target-circle state image is truncated")
    status = struct.unpack_from("<I", image, 0)[0]
    operation = struct.unpack_from("<I", image, TARGET_OPERATION_OFFSET)[0]
    if status not in (3, 4) or operation != TARGET_OPERATION_CREATE:
        return None
    sequence = struct.unpack_from("<I", image, TARGET_SEQUENCE_OFFSET)[0]
    ack = struct.unpack_from("<I", image, TARGET_ACK_SEQUENCE_OFFSET)[0]
    owner_tag = struct.unpack_from("<I", image, TARGET_OWNER_OFFSET)[0]
    entered = struct.unpack_from("<I", image, TARGET_FACTORY_ENTERED_OFFSET)[0]
    manager = struct.unpack_from("<Q", image, TARGET_RESULT_MANAGER_OFFSET)[0]
    allocator = struct.unpack_from("<Q", image, TARGET_RESULT_ALLOCATOR_OFFSET)[0]
    reserved = struct.unpack_from("<Q", image, TARGET_RESULT_RESERVED_OFFSET)[0]
    node = struct.unpack_from("<Q", image, TARGET_NODE_OFFSET)[0]
    target = struct.unpack_from("<I", image, TARGET_HANDLE_OFFSET)[0]
    observed = struct.unpack_from("<I", image, TARGET_OBSERVED_OFFSET)[0]
    palette = struct.unpack_from("<I", image, TARGET_OBSERVED_PALETTE_OFFSET)[0]
    desired_palette = struct.unpack_from(
        "<I", image, TARGET_DESIRED_PALETTE_OFFSET
    )[0]
    request_reserved = struct.unpack_from(
        "<I", image, TARGET_REQUEST_RESERVED_OFFSET
    )[0]
    if (
        not sequence
        or ack != sequence
        or owner_tag not in TARGET_OWNER_TAGS
        or entered not in (0, 1)
        or not target
        or reserved
        or request_reserved
        or not 0 <= desired_palette <= 6
    ):
        raise ValueError("terminal target-circle CREATE journal is invalid")
    if status == 3:
        if (
            not entered
            or not manager
            or not allocator
            or not node
            or observed != target
            or not 0 <= palette <= 6
            or palette != desired_palette
        ):
            raise ValueError("successful target-circle CREATE journal is incomplete")
    elif entered:
        if not manager or not allocator:
            raise ValueError("ambiguous target-circle CREATE lacks heap identity")
        if palette and not 0 <= palette <= 6:
            raise ValueError("ambiguous target-circle CREATE palette is invalid")
    elif manager or allocator or node or observed or palette:
        raise ValueError("rejected target-circle CREATE contains factory results")
    return TargetCircleCreateJournal(
        status=status,
        sequence=sequence,
        owner_tag=owner_tag,
        factory_entered=bool(entered),
        manager=manager,
        allocator=allocator,
        node=node,
        target=target,
        observed=observed,
        palette=palette,
    )


# Every router instance addresses the same process mailbox and callback slot.
# Serialize Python-side preparation/publication so two controller threads can
# never both observe idle/fresh and overwrite one another's request or cave.
_SERVICE_LOCK = threading.RLock()


def _rel32(source: int, size: int, target: int) -> bytes:
    return struct.pack("<i", int(target) - (int(source) + int(size)))


def _patch_rel32(code: bytearray, offset: int, cave: int, target_offset: int) -> None:
    source_after = int(cave) + int(offset) + 4
    target = int(cave) + int(target_offset)
    struct.pack_into("<i", code, offset, target - source_after)


def _finalize_internal_branches(
    code: bytearray,
    cave: int,
    labels: dict[str, int],
    fixups: list[tuple[int, str]],
    absolute_fixups: tuple[tuple[int, int], ...] = (),
) -> bytes:
    """Patch internal branches and relax every provably-near jump.

    The callback is deliberately dense because its transaction image and
    rollback journal share one fixed page.  This finalizer only shortens Jcc/JMP
    instructions whose original displacement already fits int8; removing bytes
    between a branch and its target can only make that displacement smaller.
    CALLs retain rel32 and every target is remapped after relaxation.
    """

    replacements: dict[int, tuple[int, int]] = {}
    for displacement, target in fixups:
        if target not in labels:
            raise RuntimeError(f"unknown target-circle dispatcher label {target!r}")
        if displacement >= 2 and code[displacement - 2] == 0x0F:
            opcode = code[displacement - 1]
            if 0x80 <= opcode <= 0x8F:
                start = displacement - 2
                delta = labels[target] - (displacement + 4)
                if -128 <= delta <= 127:
                    replacements[start] = (6, 0x70 | (opcode & 0x0F))
        elif displacement >= 1 and code[displacement - 1] == 0xE9:
            start = displacement - 1
            delta = labels[target] - (displacement + 4)
            if -128 <= delta <= 127:
                replacements[start] = (5, 0xEB)
    removed_before: list[tuple[int, int]] = []
    removed = 0
    for start, (old_size, _opcode) in sorted(replacements.items()):
        removed += old_size - 2
        removed_before.append((start + old_size, removed))

    def remap(offset: int) -> int:
        reduction = 0
        for boundary, amount in removed_before:
            if boundary <= offset:
                reduction = amount
            else:
                break
        return int(offset) - reduction

    compact = bytearray()
    cursor = 0
    while cursor < len(code):
        replacement = replacements.get(cursor)
        if replacement is None:
            compact.append(code[cursor])
            cursor += 1
            continue
        old_size, opcode = replacement
        compact += bytes((opcode, 0))
        cursor += old_size

    for displacement, target in fixups:
        target_offset = remap(labels[target])
        if displacement >= 2 and (displacement - 2) in replacements:
            start = remap(displacement - 2)
            delta = target_offset - (start + 2)
            if not -128 <= delta <= 127:
                raise RuntimeError("relaxed target-circle branch escaped int8")
            struct.pack_into("<b", compact, start + 1, delta)
            continue
        if displacement >= 1 and (displacement - 1) in replacements:
            start = remap(displacement - 1)
            delta = target_offset - (start + 2)
            if not -128 <= delta <= 127:
                raise RuntimeError("relaxed target-circle jump escaped int8")
            struct.pack_into("<b", compact, start + 1, delta)
            continue
        new_displacement = remap(displacement)
        source_after = int(cave) + new_displacement + 4
        target_address = int(cave) + target_offset
        struct.pack_into("<i", compact, new_displacement, target_address - source_after)
    for displacement, target_address in absolute_fixups:
        new_displacement = remap(displacement)
        source_after = int(cave) + new_displacement + 4
        struct.pack_into(
            "<i",
            compact,
            new_displacement,
            int(target_address) - source_after,
        )
    return bytes(compact)


def build_target_palette_selector(cave: int, state: int) -> bytes:
    """Return the guarded palette selector used by all three native loads.

    Three four-byte entry stubs place the load phase on the stack before
    entering one common leaf.  This distinction is essential: at 0x291858 the
    native function has not yet stored the requested target, while 0x291893
    runs after that store and 0x291ACE constructs a new node.  EAX always starts
    with the exact stock table lookup and changes only for the phase's complete,
    authenticated tuple.  The selector mutates no game or mailbox state.
    """

    entries = b"\x6a\x01\xeb\x08\x6a\x02\xeb\x04\x6a\x03\xeb\x00"
    common = int(cave) + len(entries)
    code = bytearray(b"\x43\x0f\xbe\x04\x3c")  # stock table lookup
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def mark(name: str) -> None:
        labels[name] = len(code)

    def branch(opcode: bytes, target: str) -> None:
        code.extend(opcode)
        fixups.append((len(code), target))
        code.extend(b"\x00" * 4)

    code += b"\x41\x52\x41\x53"  # preserve r10/r11
    code += b"\x49\xba" + struct.pack("<Q", int(state))
    code += b"\x41\x83\x3a\x02"
    branch(b"\x0f\x85", "done")
    code += b"\x41\x81\x7a" + bytes((TARGET_REQUEST_RESERVED_OFFSET,))
    code += struct.pack("<I", TARGET_PALETTE_OVERRIDE_COOKIE)
    branch(b"\x0f\x85", "done")
    # All phases identify the same request target and bounded custom palette.
    code += b"\x41\x39\x6a" + bytes((TARGET_HANDLE_OFFSET,))
    branch(b"\x0f\x85", "done")
    code += b"\x41\x83\x7a" + bytes((TARGET_DESIRED_PALETTE_OFFSET,)) + b"\x06"
    branch(b"\x0f\x87", "done")

    code += b"\x41\x83\x7a" + bytes((TARGET_OPERATION_OFFSET, TARGET_OPERATION_CREATE))
    branch(b"\x0f\x84", "create")
    code += b"\x41\x83\x7a" + bytes((TARGET_OPERATION_OFFSET, TARGET_OPERATION_RETARGET))
    branch(b"\x0f\x85", "done")
    code += b"\x49\x39\x72" + bytes((TARGET_NODE_OFFSET,))
    branch(b"\x0f\x85", "done")
    # Phase 1 replaces 0x291858 before the target store: require the exact old
    # authenticated target.  Phase 2 replaces 0x291893 after that store and
    # therefore requires the requested target.  CREATE can enter neither path.
    code += b"\x80\x7c\x24\x10\x01"
    branch(b"\x0f\x84", "retarget_command")
    code += b"\x80\x7c\x24\x10\x02"
    branch(b"\x0f\x85", "done")
    code += b"\x39\xae\x2c\x0d\x00\x00"
    branch(b"\x0f\x85", "done")
    branch(b"\xe9", "manager")

    mark("retarget_command")
    code += b"\x45\x8b\x5a" + bytes((TARGET_EXPECTED_TARGET_OFFSET,))
    code += b"\x44\x39\x9e\x2c\x0d\x00\x00"
    branch(b"\x0f\x85", "done")
    branch(b"\xe9", "manager")

    mark("create")
    code += b"\x80\x7c\x24\x10\x03"
    branch(b"\x0f\x85", "done")
    code += b"\x49\x83\x7a" + bytes((TARGET_NODE_OFFSET,)) + b"\x00"
    branch(b"\x0f\x85", "done")
    code += b"\x39\xae\x2c\x0d\x00\x00"
    branch(b"\x0f\x85", "done")

    mark("manager")
    code += b"\x4c\x8b\x1d"
    manager_displacement = len(code)
    code += b"\x00\x00\x00\x00"
    code += b"\x4d\x85\xdb"
    branch(b"\x0f\x84", "done")
    code += b"\x49\x39\xb3\xf0\x0e\x01\x00"
    branch(b"\x0f\x85", "done")
    code += b"\x41\x8b\x42" + bytes((TARGET_DESIRED_PALETTE_OFFSET,))

    mark("done")
    code += b"\x41\x5b\x41\x5a\x48\x83\xc4\x08\xc3"
    return entries + _finalize_internal_branches(
        code,
        common,
        labels,
        fixups,
        ((manager_displacement, TARGET_UI_MANAGER_GLOBAL),),
    )


def build_target_palette_hooks(service: int, state: int) -> bytes:
    """Build immutable wrappers for the three patched native palette loads."""

    image = bytearray(CODE_OFFSET - SERVICE_OFFSET)
    selector = int(service) + TARGET_PALETTE_SELECTOR_OFFSET

    command = int(service) + TARGET_PALETTE_COMMAND_HOOK_OFFSET
    body = bytearray(b"\x9c\x50\x48\x83\xec\x28")
    body += b"\xe8" + _rel32(command + len(body), 5, selector)
    # The original MOVSX writes all 64 bits of R9.  Preserve both RAX and
    # RFLAGS, and give the nested call aligned Win64 shadow space.
    body += b"\x48\x83\xc4\x28\x4c\x63\xc8\x58\x9d\xc3"
    image[
        TARGET_PALETTE_COMMAND_HOOK_OFFSET : TARGET_PALETTE_COMMAND_HOOK_OFFSET
        + len(body)
    ] = body

    field = int(service) + TARGET_PALETTE_FIELD_HOOK_OFFSET
    body = bytearray(b"\x9c\x48\x83\xec\x20")
    body += b"\xe8" + _rel32(field + len(body), 5, selector + 4)
    body += b"\x48\x83\xc4\x20\x9d\xc3"
    image[
        TARGET_PALETTE_FIELD_HOOK_OFFSET : TARGET_PALETTE_FIELD_HOOK_OFFSET
        + len(body)
    ] = body

    create = int(service) + TARGET_PALETTE_CREATE_HOOK_OFFSET
    body = bytearray(b"\x9c\x50\x56\x48\x83\xec\x20\x48\x8b\xf3")
    body += b"\xe8" + _rel32(create + len(body), 5, selector + 8)
    body += b"\x48\x83\xc4\x20\x41\x89\xc0\x5e\x58\x9d\xc3"
    image[
        TARGET_PALETTE_CREATE_HOOK_OFFSET : TARGET_PALETTE_CREATE_HOOK_OFFSET
        + len(body)
    ] = body

    return bytes(image)


def build_target_dispatcher(cave: int, state: int) -> bytes:
    """Build one immutable callback for create and retarget requests."""

    code = bytearray()
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def mark(name: str) -> None:
        labels[name] = len(code)

    def branch(opcode: bytes, target: str) -> None:
        code.extend(opcode)
        fixups.append((len(code), target))
        code.extend(b"\x00" * 4)

    def call(target: str) -> None:
        branch(b"\xe8", target)

    def ownership_checksum_to_eax() -> None:
        """Emit the V6 ownership checksum for the record addressed by R15."""

        code.extend(b"\x41\x8b\x07")  # eax <- owner
        code.extend(b"\x41\x33\x47\x04")  # xor token
        for offset in (0x08, 0x10, 0x18):
            code.extend(b"\x49\x8b\x57" + bytes((offset,)))
            code.extend(b"\x31\xd0\x48\xc1\xea\x20\x31\xd0")
        code.extend(b"\x41\x33\x47\x20")  # xor target
        code.extend(b"\x41\x33\x47\x24")  # xor palette
        code.extend(b"\x35" + struct.pack("<I", _OWNERSHIP_CHECKSUM_SALT))

    def verify_ownership_checksum(error_label: str) -> None:
        ownership_checksum_to_eax()
        code.extend(b"\x41\x39\x47\x28")
        branch(b"\x0f\x85", error_label)
        code.extend(b"\xf7\xd0\x41\x39\x47\x2c")
        branch(b"\x0f\x85", error_label)

    def publish_ownership_checksum() -> None:
        ownership_checksum_to_eax()
        code.extend(b"\x41\x89\x47\x28\xf7\xd0\x41\x89\x47\x2c")

    # State 1 is the sole request publication; state 2 is written before any
    # native pointer is sampled.
    code += b"\x48\xb8" + struct.pack("<Q", int(state))
    code += b"\x83\x38\x01"
    branch(b"\x0f\x85", "idle")
    code += b"\xc7\x00\x02\x00\x00\x00"

    # Preserve every nonvolatile register used by the authenticated owned-node
    # transaction.  The 0x108-byte frame preserves Win64 alignment/shadow space
    # and reserves an exact pre-retarget rollback image below the native calls.
    code += (
        b"\x53\x55\x56\x57"
        b"\x41\x54\x41\x55\x41\x56\x41\x57"
        b"\x48\x81\xec\x08\x01\x00\x00"
    )
    code += b"\x48\xbd" + struct.pack("<Q", int(state))
    code += b"\x8b\x45" + bytes((TARGET_OPERATION_OFFSET,))
    code += b"\x83\xf8" + bytes((TARGET_OPERATION_CREATE,))
    branch(b"\x0f\x84", "create")
    code += b"\x83\xf8" + bytes((TARGET_OPERATION_RETARGET,))
    branch(b"\x0f\x84", "owned")
    code += b"\x83\xf8" + bytes((TARGET_OPERATION_RETIRE,))
    branch(b"\x0f\x84", "owned")
    code += b"\x83\xf8" + bytes((TARGET_OPERATION_RECONCILE,))
    branch(b"\x0f\x84", "owned")
    branch(b"\xe9", "error")

    # CREATE ---------------------------------------------------------------
    mark("create")
    code += b"\x48\xb8" + struct.pack("<Q", TARGET_UI_MANAGER_GLOBAL)
    code += b"\x48\x8b\x18"
    code += b"\x8b\x7d" + bytes((TARGET_HANDLE_OFFSET,))
    code += b"\x48\x85\xdb"
    branch(b"\x0f\x84", "create_gate_reject")
    code += b"\x85\xff"
    branch(b"\x0f\x84", "create_gate_reject")

    # A CREATE is an allocating transaction.  Bind it to one explicit owner so
    # a terminal result can remain resident until that owner proves and claims
    # the allocation, including across a helper restart.
    code += b"\x8b\x45" + bytes((TARGET_OWNER_OFFSET,))
    code += b"\x3d" + struct.pack("<I", P1_TARGET_OWNER_TAG)
    branch(b"\x0f\x84", "create_owner_valid")
    code += b"\x3d" + struct.pack("<I", P2_TARGET_OWNER_TAG)
    branch(b"\x0f\x84", "create_owner_valid")
    code += b"\x3d" + struct.pack("<I", P3_TARGET_OWNER_TAG)
    branch(b"\x0f\x85", "create_gate_reject")

    mark("create_owner_valid")
    code += b"\x83\x7d" + bytes((TARGET_DESIRED_PALETTE_OFFSET,)) + b"\x06"
    branch(b"\x0f\x87", "create_gate_reject")
    code += b"\x8b\x45" + bytes((TARGET_VARIANT_OFFSET,))
    code += b"\x83\xf8\x01"
    branch(b"\x0f\x84", "create_variant_valid")
    code += b"\x83\xf8\x02"
    branch(b"\x0f\x85", "create_gate_reject")

    mark("create_variant_valid")
    branch(b"\xe9", "create_context")

    mark("create_gate_reject")
    branch(b"\xe9", "create_reject")

    mark("create_context")
    code += b"\x48\x8b\x93\x00\x0f\x01\x00"
    code += b"\x48\x89\x55\x28"
    code += b"\x48\x8b\xb3\xf0\x0e\x01\x00"
    code += b"\x48\x89\x75\x30"

    # The native factory mutates every pre-existing list member before it
    # appends.  Only enter it from a coherent, settled view: an empty list has
    # no current node; a non-empty list has one or two live nodes and current
    # names one of them.  A retiring (5), newly-created (1), full or torn view
    # is retried after the native UI tick instead of being resurrected.
    code += b"\x48\x31\xc0"
    code += b"\x48\x89\x45\x38"
    code += b"\x48\x8b\x45\x28"
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "create_empty_view")
    code += b"\x48\x85\xf6"
    branch(b"\x0f\x84", "create_view_reject_a")
    code += b"\x66\x83\xb8\x54\x0d\x00\x00\x02"
    branch(b"\x0f\x85", "create_view_reject_a")
    code += b"\x48\x83\xb8\x68\x0d\x00\x00\x00"
    branch(b"\x0f\x85", "create_view_reject_a")
    code += b"\x48\x8b\x90\x70\x0d\x00\x00"
    code += b"\x48\x89\x55\x38"
    code += b"\x48\x85\xd2"
    branch(b"\x0f\x84", "create_one_old_node")
    code += b"\x66\x83\xba\x54\x0d\x00\x00\x02"
    branch(b"\x0f\x85", "create_view_reject_a")
    code += b"\x48\x39\x82\x68\x0d\x00\x00"
    branch(b"\x0f\x85", "create_view_reject_a")
    code += b"\x48\x83\xba\x70\x0d\x00\x00\x00"
    branch(b"\x0f\x85", "create_view_reject_a")

    branch(b"\xe9", "create_view_compare_current")

    mark("create_view_reject_a")
    branch(b"\xe9", "create_reject")

    mark("create_view_compare_current")
    code += b"\x48\x39\xc6"
    branch(b"\x0f\x84", "create_view_valid")
    code += b"\x48\x39\xd6"
    branch(b"\x0f\x85", "create_view_reject_a")
    branch(b"\xe9", "create_view_valid")

    mark("create_one_old_node")
    code += b"\x48\x39\xc6"
    branch(b"\x0f\x85", "create_view_reject_a")
    branch(b"\xe9", "create_view_valid")

    mark("create_empty_view")
    code += b"\x48\x85\xf6"
    branch(b"\x0f\x85", "create_view_reject_a")

    mark("create_view_valid")
    code += b"\x48\x8b\x83\xc0\x0f\x01\x00"
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "create_view_reject_b")
    code += b"\x48\x89\x44\x24\x20"

    # Recheck every identity/link immediately before the first lifecycle write.
    # This closes the Python-snapshot -> native-tick load/KO transition window.
    code += b"\x49\xbb" + struct.pack("<Q", TARGET_UI_MANAGER_GLOBAL)
    code += b"\x49\x39\x1b"
    branch(b"\x0f\x85", "create_view_reject_b")
    code += b"\x48\x8b\x45\x28"
    code += b"\x48\x39\x83\x00\x0f\x01\x00"
    branch(b"\x0f\x85", "create_view_reject_b")
    code += b"\x48\x8b\x55\x30"
    code += b"\x48\x39\x93\xf0\x0e\x01\x00"
    branch(b"\x0f\x85", "create_view_reject_b")
    code += b"\x48\x8b\x45\x28"
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "protect_create_view")
    code += b"\x66\x83\xb8\x54\x0d\x00\x00\x02"
    branch(b"\x0f\x85", "create_view_reject_b")
    code += b"\x48\x83\xb8\x68\x0d\x00\x00\x00"
    branch(b"\x0f\x85", "create_view_reject_b")
    code += b"\x48\x8b\x55\x38"
    code += b"\x48\x39\x90\x70\x0d\x00\x00"
    branch(b"\x0f\x85", "create_view_reject_b")
    code += b"\x48\x85\xd2"
    branch(b"\x0f\x84", "protect_create_view")
    code += b"\x66\x83\xba\x54\x0d\x00\x00\x02"
    branch(b"\x0f\x85", "create_view_reject_b")
    code += b"\x48\x39\x82\x68\x0d\x00\x00"
    branch(b"\x0f\x85", "create_view_reject_b")
    code += b"\x48\x83\xba\x70\x0d\x00\x00\x00"
    branch(b"\x0f\x85", "create_view_reject_b")

    branch(b"\xe9", "protect_create_view")

    mark("create_view_reject_b")
    branch(b"\xe9", "create_reject")

    mark("protect_create_view")
    # 0x2917FE clears manager+0x10F68 bit 3, and 0x28C4F0 mutates D30,
    # D4C and D54 on every pre-existing circle while appending.  Preserve the
    # two admitted old nodes in nonvolatile registers (plus the manager flag in
    # the rollback frame) before the native function can observe them.
    code += b"\x8b\x83\x68\x0f\x01\x00"
    code += b"\x89\x84\x24\x00\x01\x00\x00"
    code += b"\x48\x8b\x45\x28"
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "call_create")
    code += b"\x44\x8b\xa0\x30\x0d\x00\x00"
    code += b"\x44\x8b\xb8\x4c\x0d\x00\x00"
    code += b"\x48\x8b\x55\x38"
    code += b"\x48\x85\xd2"
    branch(b"\x0f\x84", "call_create")
    code += b"\x44\x8b\xb2\x30\x0d\x00\x00"
    code += b"\x44\x8b\xaa\x4c\x0d\x00\x00"

    mark("call_create")
    code += b"\xc7\x45" + bytes((TARGET_FACTORY_ENTERED_OFFSET,)) + b"\x01\x00\x00\x00"
    # 0x2917E0 retargets manager+0x10EF0 whenever that pointer names a live
    # node; it reaches its allocating branch only when current is null.  The
    # complete list and current identity were saved and re-proved above, so
    # hide current only for this authenticated game-thread call.  The native
    # function still sees the real head, appends the new node to that list and
    # the exact saved current is restored below before the callback publishes.
    code += b"\x45\x33\xc0"
    code += b"\x4c\x89\x83\xf0\x0e\x01\x00"
    # Only this authenticated game-thread interval authorizes the three
    # palette-load gates inside 0x2917E0.  CREATE is intercepted before the new
    # node is linked, so its first render command and D7C publication are
    # custom from birth.
    code += b"\xc7\x45" + bytes((TARGET_REQUEST_RESERVED_OFFSET,))
    code += struct.pack("<I", TARGET_PALETTE_OVERRIDE_COOKIE)
    code += b"\x48\x8b\xcb\x8b\xd7"
    code += b"\x44\x8b\x4d" + bytes((TARGET_VARIANT_OFFSET,))
    code += b"\x49\xba" + struct.pack("<Q", TARGET_CIRCLE_RETARGET)
    code += b"\x41\xff\xd2"
    code += b"\xc7\x45" + bytes((TARGET_REQUEST_RESERVED_OFFSET,)) + b"\x00\x00\x00\x00"
    code += b"\x48\x8b\xb3\xf0\x0e\x01\x00"
    code += b"\x48\x89\x75" + bytes((TARGET_NODE_OFFSET,))

    # Restore every native-cleanup field before any terminal edge can publish.
    # Nodes were proven exact immediately above; native ABI preserves R12-R15.
    code += b"\x8b\x84\x24\x00\x01\x00\x00"
    code += b"\x89\x83\x68\x0f\x01\x00"
    code += b"\x48\x8b\x45\x28\x48\x85\xc0"
    branch(b"\x0f\x84", "restore_create_cleanup_done")
    code += b"\x44\x89\xa0\x30\x0d\x00\x00"
    code += b"\x44\x89\xb8\x4c\x0d\x00\x00"
    code += b"\x48\x8b\x55\x38\x48\x85\xd2"
    branch(b"\x0f\x84", "restore_create_cleanup_done")
    code += b"\x44\x89\xb2\x30\x0d\x00\x00"
    code += b"\x44\x89\xaa\x4c\x0d\x00\x00"

    mark("restore_create_cleanup_done")

    # Restore the exact settled lifecycle admitted above before testing the
    # result.  No state 1/3/4/5 node can reach this path, so retirement is never
    # converted back into a live circle.
    code += b"\x48\x8b\x45\x28\x48\x85\xc0"
    branch(b"\x0f\x84", "restore_create_manager")
    code += b"\x66\xc7\x80\x54\x0d\x00\x00\x02\x00"
    code += b"\x48\xc7\x80\x68\x0d\x00\x00\x00\x00\x00\x00"
    code += b"\x48\x8b\x55\x38\x48\x85\xd2"
    branch(b"\x0f\x84", "restore_create_manager")
    code += b"\x66\xc7\x82\x54\x0d\x00\x00\x02\x00"
    code += b"\x48\x89\x82\x68\x0d\x00\x00"

    mark("restore_create_manager")
    code += b"\x48\x8b\x55\x28"
    code += b"\x48\x85\xd2"
    # With no pre-existing list, 0x2917E0 has just published the first node as
    # both head and current.  Restoring the saved zero pointers here detaches
    # that allocation forever and eventually exhausts FF12's nine-block UI
    # pool.  Only restore a foreign list that actually existed before CREATE.
    branch(b"\x0f\x84", "keep_created_manager")
    code += b"\x48\x89\x93\x00\x0f\x01\x00"
    code += b"\x48\x8b\x55\x30"
    code += b"\x48\x89\x93\xf0\x0e\x01\x00"

    mark("keep_created_manager")
    # Prove this invocation appended a genuinely new, linked node and that the
    # manager/allocator identities used by the callback did not change.  A
    # post-factory uncertainty remains terminal and cannot be overwritten.
    code += b"\x49\xbb" + struct.pack("<Q", TARGET_UI_MANAGER_GLOBAL)
    code += b"\x49\x39\x1b"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x48\x8b\x44\x24\x20"
    code += b"\x48\x39\x83\xc0\x0f\x01\x00"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x48\x85\xf6"
    branch(b"\x0f\x84", "create_factory_error")

    code += b"\x48\x8b\x45\x28"
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "validate_empty_create_result")
    code += b"\x48\x39\xc6"
    branch(b"\x0f\x84", "create_factory_error")
    code += b"\x48\x83\xb8\x68\x0d\x00\x00\x00"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x48\x39\x83\x00\x0f\x01\x00"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x48\x8b\x55\x30"
    code += b"\x48\x39\x93\xf0\x0e\x01\x00"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x48\x8b\x55\x38"
    code += b"\x48\x85\xd2"
    branch(b"\x0f\x84", "validate_one_old_node_result")
    code += b"\x48\x39\xd6"
    branch(b"\x0f\x84", "create_factory_error")
    code += b"\x48\x39\x82\x68\x0d\x00\x00"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x48\x39\x90\x70\x0d\x00\x00"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x48\x39\xb2\x70\x0d\x00\x00"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x48\x8b\x86\x68\x0d\x00\x00"
    code += b"\x48\x39\xd0"
    branch(b"\x0f\x85", "create_factory_error")
    branch(b"\xe9", "validate_new_node_tail")

    mark("validate_one_old_node_result")
    code += b"\x48\x39\xb0\x70\x0d\x00\x00"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x48\x8b\x96\x68\x0d\x00\x00"
    code += b"\x48\x39\xc2"
    branch(b"\x0f\x85", "create_factory_error")
    branch(b"\xe9", "validate_new_node_tail")

    mark("validate_empty_create_result")
    code += b"\x48\x39\xb3\x00\x0f\x01\x00"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x48\x39\xb3\xf0\x0e\x01\x00"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x48\x83\xbe\x68\x0d\x00\x00\x00"
    branch(b"\x0f\x85", "create_factory_error")

    mark("validate_new_node_tail")
    code += b"\x48\x83\xbe\x70\x0d\x00\x00\x00"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x0f\xb7\x86\x54\x0d\x00\x00"
    code += b"\x83\xf8\x01"
    branch(b"\x0f\x84", "validate_created_target")
    code += b"\x83\xf8\x02"
    branch(b"\x0f\x85", "create_factory_error")

    mark("validate_created_target")
    code += b"\x8b\x96\x2c\x0d\x00\x00"
    code += b"\x39\xfa"
    branch(b"\x0f\x85", "create_factory_error")
    # The load gates already made the first native command custom before this
    # node was linked.  Rebuild once more after the complete append proof so a
    # corrupted/mismatched factory return cannot leave a blue orphan behind.
    code += b"\x48\x83\xbe\x00\x0d\x00\x00\x00"
    branch(b"\x0f\x84", "create_factory_error")
    call("apply_palette")
    code += b"\x8b\x96\x2c\x0d\x00\x00"
    code += b"\x8b\x86\x7c\x0d\x00\x00"
    code += b"\x3b\x45" + bytes((TARGET_DESIRED_PALETTE_OFFSET,))
    branch(b"\x0f\x85", "create_factory_error")
    # Validate the actual qword consumed by rendering, matching 0x271650:
    # resource[0x30] + (palette << 38) at node+0xBD0.
    code += b"\x8b\xc8\x48\xc1\xe1\x26"
    code += b"\x48\x8b\x86\x00\x0d\x00\x00"
    code += b"\x48\x03\x48\x30"
    code += b"\x48\x39\x8e\xd0\x0b\x00\x00"
    branch(b"\x0f\x85", "create_factory_error")
    code += b"\x89\x55" + bytes((TARGET_OBSERVED_OFFSET,))
    code += b"\x8b\x45" + bytes((TARGET_DESIRED_PALETTE_OFFSET,))
    code += b"\x89\x45" + bytes((TARGET_OBSERVED_PALETTE_OFFSET,))
    branch(b"\xe9", "create_success")

    mark("create_factory_error")
    code += b"\x48\x89\x5d" + bytes((TARGET_RESULT_MANAGER_OFFSET,))
    code += b"\x48\x8b\x44\x24\x20"
    code += b"\x48\x89\x45" + bytes((TARGET_RESULT_ALLOCATOR_OFFSET,))
    code += b"\x48\xc7\x45" + bytes((TARGET_RESULT_RESERVED_OFFSET,)) + b"\x00\x00\x00\x00"
    branch(b"\xe9", "error")

    mark("create_success")
    code += b"\x48\x89\x5d" + bytes((TARGET_RESULT_MANAGER_OFFSET,))
    code += b"\x48\x8b\x44\x24\x20"
    code += b"\x48\x89\x45" + bytes((TARGET_RESULT_ALLOCATOR_OFFSET,))
    code += b"\x48\xc7\x45" + bytes((TARGET_RESULT_RESERVED_OFFSET,)) + b"\x00\x00\x00\x00"
    branch(b"\xe9", "success")

    mark("create_reject")
    code += b"\x48\x31\xc0"
    code += b"\x48\x89\x45" + bytes((TARGET_NODE_OFFSET,))
    code += b"\x89\x45" + bytes((TARGET_OBSERVED_OFFSET,))
    code += b"\x89\x45" + bytes((TARGET_FACTORY_ENTERED_OFFSET,))
    code += b"\x48\x89\x45" + bytes((TARGET_RESULT_MANAGER_OFFSET,))
    code += b"\x48\x89\x45" + bytes((TARGET_RESULT_ALLOCATOR_OFFSET,))
    code += b"\x48\x89\x45" + bytes((TARGET_RESULT_RESERVED_OFFSET,))
    code += b"\x89\x45" + bytes((TARGET_OBSERVED_PALETTE_OFFSET,))
    code += b"\x89\x45" + bytes((TARGET_DESIRED_PALETTE_OFFSET,))
    code += b"\x89\x45" + bytes((TARGET_REQUEST_RESERVED_OFFSET,))
    branch(b"\xe9", "error")

    # AUTHENTICATED OWNED-NODE OPERATIONS ---------------------------------
    # Python publishes no destructive node write.  The game-thread callback
    # binds every update/reconcile/retirement to the exact resident
    # owner+token+manager+allocator+node record and a coherent live list.
    mark("owned")
    code += b"\x48\xb8" + struct.pack("<Q", TARGET_UI_MANAGER_GLOBAL)
    code += b"\x48\x8b\x18"
    code += b"\x48\x8b\x75" + bytes((TARGET_NODE_OFFSET,))
    code += b"\x48\x85\xdb"
    branch(b"\x0f\x84", "error")
    code += b"\x48\x85\xf6"
    branch(b"\x0f\x84", "error")
    code += b"\x48\x8b\x45" + bytes((TARGET_EXPECTED_MANAGER_OFFSET,))
    code += b"\x48\x39\xd8"
    branch(b"\x0f\x85", "error")
    code += b"\x4c\x8b\x6d" + bytes((TARGET_EXPECTED_ALLOCATOR_OFFSET,))
    code += b"\x4d\x85\xed"
    branch(b"\x0f\x84", "error")
    code += b"\x4c\x39\xab\xc0\x0f\x01\x00"
    branch(b"\x0f\x85", "error")

    # Resolve the fixed owner slot without accepting a palette/address guess.
    code += b"\x8b\x45" + bytes((TARGET_OWNER_OFFSET,))
    code += b"\x3d" + struct.pack("<I", P1_TARGET_OWNER_TAG)
    branch(b"\x0f\x84", "owned_slot_p1")
    code += b"\x3d" + struct.pack("<I", P2_TARGET_OWNER_TAG)
    branch(b"\x0f\x84", "owned_slot_p2")
    code += b"\x3d" + struct.pack("<I", P3_TARGET_OWNER_TAG)
    branch(b"\x0f\x85", "error")
    code += b"\x4c\x8d\xbd" + struct.pack(
        "<I",
        TARGET_OWNERSHIP_RECORDS_OFFSET + 2 * TARGET_OWNERSHIP_RECORD_SIZE,
    )
    branch(b"\xe9", "owned_slot_ready")

    mark("owned_slot_p2")
    code += b"\x4c\x8d\xbd" + struct.pack(
        "<I",
        TARGET_OWNERSHIP_RECORDS_OFFSET + TARGET_OWNERSHIP_RECORD_SIZE,
    )
    branch(b"\xe9", "owned_slot_ready")

    mark("owned_slot_p1")
    code += b"\x4c\x8d\xbd" + struct.pack(
        "<I", TARGET_OWNERSHIP_RECORDS_OFFSET
    )

    mark("owned_slot_ready")
    code += b"\x8b\x45" + bytes((TARGET_OPERATION_OFFSET,))
    code += b"\x83\xf8" + bytes((TARGET_OPERATION_RECONCILE,))
    branch(b"\x0f\x84", "owned_initial_identity")
    code += b"\x45\x31\xd2"  # r10d=0: require exact target/palette
    call("validate_owned_live")
    branch(b"\xe9", "owned_initial_checked")

    mark("owned_initial_identity")
    code += b"\x41\xba\x01\x00\x00\x00"
    call("validate_owned_live")

    mark("owned_initial_checked")
    code += b"\x85\xc0"
    branch(b"\x0f\x84", "error")
    code += b"\x8b\x45" + bytes((TARGET_OPERATION_OFFSET,))
    code += b"\x83\xf8" + bytes((TARGET_OPERATION_RETARGET,))
    branch(b"\x0f\x84", "owned_update")
    code += b"\x83\xf8" + bytes((TARGET_OPERATION_RETIRE,))
    branch(b"\x0f\x84", "owned_retire")
    code += b"\x83\xf8" + bytes((TARGET_OPERATION_RECONCILE,))
    branch(b"\x0f\x84", "owned_reconcile")
    branch(b"\xe9", "error")

    mark("owned_update")
    code += b"\x8b\x7d" + bytes((TARGET_HANDLE_OFFSET,))
    code += b"\x85\xff"
    branch(b"\x0f\x84", "error")
    code += b"\x8b\x45" + bytes((TARGET_DESIRED_PALETTE_OFFSET,))
    code += b"\x83\xf8\x06"
    branch(b"\x0f\x87", "error")
    # 0x271650 rebuilds the render command from this resource.  Prove it
    # before either the native retarget or our palette-specific rebuild can
    # touch the authenticated node.
    code += b"\x48\x83\xbe\x00\x0d\x00\x00\x00"
    branch(b"\x0f\x84", "error")
    # Snapshot every byte the existing-node branch of 0x2917E0 can mutate.
    # The image stays on this callback's stack and is used only after a second
    # authenticated same-node/list proof; no Python write participates.
    code += b"\x8b\x83\x68\x0f\x01\x00"
    code += b"\x89\x84\x24\x00\x01\x00\x00"
    for stack_offset, node_offset, size in (
        (0x30, 0xBB0, 0x30),
        (0x60, 0xC80, 0x30),
        (0x90, 0xD10, 0x70),
    ):
        if stack_offset < 0x80:
            code += b"\x48\x8d\x4c\x24" + bytes((stack_offset,))
        else:
            code += b"\x48\x8d\x8c\x24" + struct.pack("<I", stack_offset)
        code += b"\x48\x8d\x96" + struct.pack("<I", node_offset)
        code += b"\x41\xb8" + struct.pack("<I", size)
        call("copy_bytes")
    code += b"\x3b\x7d" + bytes((TARGET_EXPECTED_TARGET_OFFSET,))
    branch(b"\x0f\x84", "owned_apply_palette_settled")
    code += b"\x8b\x45" + bytes((TARGET_VARIANT_OFFSET,))
    code += b"\x83\xf8\x01"
    branch(b"\x0f\x84", "owned_call_retarget")
    code += b"\x83\xf8\x02"
    branch(b"\x0f\x85", "error")

    mark("owned_call_retarget")
    code += b"\x4c\x8b\xb3\x00\x0f\x01\x00"  # r14=head
    code += b"\x4c\x8b\xa3\xf0\x0e\x01\x00"  # r12=current
    code += b"\x48\x89\xb3\x00\x0f\x01\x00"
    code += b"\x48\x89\xb3\xf0\x0e\x01\x00"
    code += b"\xc7\x45" + bytes((TARGET_REQUEST_RESERVED_OFFSET,))
    code += struct.pack("<I", TARGET_PALETTE_OVERRIDE_COOKIE)
    code += b"\x48\x8b\xcb\x8b\xd7\x45\x33\xc0"
    code += b"\x44\x8b\x4d" + bytes((TARGET_VARIANT_OFFSET,))
    code += b"\x49\xba" + struct.pack("<Q", TARGET_CIRCLE_RETARGET)
    code += b"\x41\xff\xd2"
    # Disarm before every post-call proof/error edge.  The hooks can therefore
    # never authorize a later stock invocation from a stale completed request.
    code += b"\xc7\x45" + bytes((TARGET_REQUEST_RESERVED_OFFSET,)) + b"\x00\x00\x00\x00"
    code += b"\x4c\x89\xb3\x00\x0f\x01\x00"
    code += b"\x4c\x89\xa3\xf0\x0e\x01\x00"
    code += b"\x8b\x84\x24\x00\x01\x00\x00"
    code += b"\x89\x83\x68\x0f\x01\x00"

    # A real 0x2917E0 retarget deliberately leaves the node in lifecycle 1
    # until the immediately-following native UI tick.  V5 required lifecycle
    # 2 here, rejected the otherwise valid transaction and consequently left
    # the native-blue render command visible for that tick.  Mode 2 proves the
    # exact authenticated heap/list plus this one expected transition, while
    # every foreign list member must remain settled in lifecycle 2.
    # Mode 5 first proves that rollback would still name the same authenticated
    # node/list while accepting any native partial lifecycle 0..2 and loose
    # fields.  If that identity proof is lost, fail closed without touching a
    # potentially foreign object.  The palette gates still guarantee that no
    # native-blue command was ever emitted.
    code += b"\x41\xba\x05\x00\x00\x00"
    call("validate_owned_live")
    code += b"\x85\xc0"
    branch(b"\x0f\x84", "error")
    code += b"\x41\xba\x03\x00\x00\x00"
    call("validate_owned_live")
    code += b"\x85\xc0"
    branch(b"\x0f\x84", "owned_update_rollback")
    branch(b"\xe9", "owned_publish_proven")

    mark("owned_apply_palette_settled")
    code += b"\x41\xbe\x04\x00\x00\x00"  # settled post-apply mode

    mark("owned_apply_palette")
    # 0x2917E0 writes both node+0xD7C and the packed render command at
    # node+0xBB0 from the native palette table.  Correcting only 0xD7C is one
    # frame too late: the original UI tick consumes the already-built blue
    # command.  Rebuild the same command on the game thread with the requested
    # palette before returning to that tick.  No global table or stock node is
    # modified.
    call("apply_palette")
    # Mode 3 proves lifecycle 1 plus requested target/palette after a native
    # retarget; mode 4 proves lifecycle 2 plus the same fields for a
    # palette-only update.  Sidecar publication happens only after this final
    # post-mutation proof.
    code += b"\x45\x8b\xd6"
    call("validate_owned_live")
    code += b"\x85\xc0"
    branch(b"\x0f\x84", "owned_update_rollback")

    mark("owned_publish_proven")
    call("publish_live_ownership")
    code += b"\x85\xc0"
    branch(b"\x0f\x84", "error")
    branch(b"\xe9", "success")

    mark("owned_update_rollback")
    # Restore the complete exact pre-operation image only after mode 5 proved
    # that the same owner/token/heap/list member remains in place.  This covers
    # native no-op/partial returns without leaving lifecycle 1 or a half-built
    # command behind.  Foreign/torn identity is never written speculatively.
    for stack_offset, node_offset, size in (
        (0x30, 0xBB0, 0x30),
        (0x60, 0xC80, 0x30),
        (0x90, 0xD10, 0x70),
    ):
        code += b"\x48\x8d\x8e" + struct.pack("<I", node_offset)
        if stack_offset < 0x80:
            code += b"\x48\x8d\x54\x24" + bytes((stack_offset,))
        else:
            code += b"\x48\x8d\x94\x24" + struct.pack("<I", stack_offset)
        code += b"\x41\xb8" + struct.pack("<I", size)
        call("copy_bytes")
    code += b"\x8b\x84\x24\x00\x01\x00\x00"
    code += b"\x89\x83\x68\x0f\x01\x00"
    code += b"\x45\x31\xd2"
    call("validate_owned_live")
    code += b"\x85\xc0"
    branch(b"\x0f\x84", "error")
    branch(b"\xe9", "error")

    mark("owned_reconcile")
    call("publish_live_ownership")
    code += b"\x85\xc0"
    branch(b"\x0f\x84", "error")
    branch(b"\xe9", "success")

    mark("owned_retire")
    code += b"\x41\x8b\x47\x20"
    code += b"\x89\x45" + bytes((TARGET_OBSERVED_OFFSET,))
    code += b"\x41\x8b\x47\x24"
    code += b"\x89\x45" + bytes((TARGET_OBSERVED_PALETTE_OFFSET,))
    # State 5 is published before the sidecar is cleared.  No crash point can
    # therefore leave a live, unowned allocation.
    code += b"\x66\xc7\x86\x54\x0d\x00\x00\x05\x00"
    code += b"\x48\x31\xc0"
    for offset in (0x00, 0x08, 0x10, 0x18, 0x20, 0x28):
        if offset:
            code += b"\x49\x89\x47" + bytes((offset,))
        else:
            code += b"\x49\x89\x07"
    branch(b"\xe9", "success")

    mark("success")
    code += b"\xb8\x03\x00\x00\x00"
    branch(b"\xe9", "publish")

    mark("error")
    code += b"\xb8\x04\x00\x00\x00"

    mark("publish")
    code += b"\xc7\x45" + bytes((TARGET_REQUEST_RESERVED_OFFSET,)) + b"\x00\x00\x00\x00"
    code += b"\x8b\x55" + bytes((TARGET_SEQUENCE_OFFSET,))
    code += b"\x89\x55" + bytes((TARGET_ACK_SEQUENCE_OFFSET,))
    # State is the final mailbox access.  Once Python observes 3/4, the
    # callback touches no code or data that a later request can reuse.
    code += b"\x89\x45\x00"
    code += (
        b"\x48\x81\xc4\x08\x01\x00\x00"
        b"\x41\x5f\x41\x5e\x41\x5d\x41\x5c"
        b"\x5f\x5e\x5d\x5b\xc3"
    )

    mark("idle")
    code += b"\xc3"

    # Internal validation subroutines --------------------------------------
    # validate_ownership_record: R15 names the selected resident record;
    # RBX/R13/RSI carry expected manager/allocator/node.  EAX returns 1/0.
    mark("validate_ownership_record")
    code += b"\x8b\x45" + bytes((TARGET_OWNER_OFFSET,))
    code += b"\x41\x39\x07"
    branch(b"\x0f\x85", "ownership_record_invalid")
    code += b"\x8b\x45" + bytes((TARGET_AUTH_TOKEN_OFFSET,))
    code += b"\x85\xc0"
    branch(b"\x0f\x84", "ownership_record_invalid")
    code += b"\x41\x39\x47\x04"
    branch(b"\x0f\x85", "ownership_record_invalid")
    code += b"\x49\x39\x5f\x08"
    branch(b"\x0f\x85", "ownership_record_invalid")
    code += b"\x4d\x39\x6f\x10"
    branch(b"\x0f\x85", "ownership_record_invalid")
    code += b"\x49\x39\x77\x18"
    branch(b"\x0f\x85", "ownership_record_invalid")
    code += b"\x8b\x45" + bytes((TARGET_EXPECTED_TARGET_OFFSET,))
    code += b"\x85\xc0"
    branch(b"\x0f\x84", "ownership_record_invalid")
    code += b"\x41\x39\x47\x20"
    branch(b"\x0f\x85", "ownership_record_invalid")
    code += b"\x8b\x45" + bytes((TARGET_EXPECTED_PALETTE_OFFSET,))
    code += b"\x83\xf8\x06"
    branch(b"\x0f\x87", "ownership_record_invalid")
    code += b"\x41\x39\x47\x24"
    branch(b"\x0f\x85", "ownership_record_invalid")
    verify_ownership_checksum("ownership_record_invalid")
    code += b"\xb8\x01\x00\x00\x00\xc3"

    mark("ownership_record_invalid")
    code += b"\x31\xc0\xc3"

    # validate_owned_live modes:
    #   0: settled lifecycle 2, target/palette match the ownership record;
    #   1: settled lifecycle 2, fields may differ for restart reconciliation;
    #   2: lifecycle 1 on the owned node, loose fields immediately after the
    #      native retarget (all foreign members remain settled);
    #   3: lifecycle 1 and fields match the requested target/palette;
    #   4: lifecycle 2 and fields match the requested target/palette;
    #   5: rollback identity, allowing owned lifecycle 0..2 and loose fields.
    # Every mode authenticates the same owner/token/manager/allocator/node and
    # coherent list before returning success.
    mark("validate_owned_live")
    code += b"\x48\xb8" + struct.pack("<Q", TARGET_UI_MANAGER_GLOBAL)
    code += b"\x48\x39\x18"
    branch(b"\x0f\x85", "owned_live_invalid")
    code += b"\x4c\x39\xab\xc0\x0f\x01\x00"
    branch(b"\x0f\x85", "owned_live_invalid")
    # This internal routine is itself a Win64 caller.  Reserve its own aligned
    # shadow frame instead of borrowing the parent callback's return area.
    code += b"\x48\x83\xec\x28"
    call("validate_ownership_record")
    code += b"\x48\x83\xc4\x28"
    code += b"\x85\xc0"
    branch(b"\x0f\x84", "owned_live_invalid")

    code += b"\x4c\x8b\x8b\x00\x0f\x01\x00"  # r9=head
    code += b"\x4c\x89\xc8"  # rax=current node
    code += b"\x31\xd2"  # rdx=expected previous
    code += b"\x45\x31\xc0"  # r8d=owned node found
    code += b"\xb9\x08\x00\x00\x00"

    mark("owned_list_loop")
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "owned_list_done")
    code += b"\x48\x39\xf0"
    branch(b"\x0f\x84", "owned_member_lifecycle")
    # Foreign members are never admitted in transition.  A KO, Stone or UI
    # rebuild can leave another circle transitional while links still look
    # coherent; retry on a later tick.
    code += b"\x66\x83\xb8\x54\x0d\x00\x00\x02"
    branch(b"\x0f\x85", "owned_live_invalid")
    branch(b"\xe9", "owned_member_lifecycle_valid")

    mark("owned_member_lifecycle")
    code += b"\x41\x83\xfa\x05"
    branch(b"\x0f\x84", "owned_member_lifecycle_rollback")
    code += b"\x41\x83\xfa\x02"
    branch(b"\x0f\x82", "owned_member_lifecycle_settled")
    code += b"\x41\x83\xfa\x03"
    branch(b"\x0f\x87", "owned_member_lifecycle_settled")
    code += b"\x66\x83\xb8\x54\x0d\x00\x00\x01"
    branch(b"\x0f\x85", "owned_live_invalid")
    branch(b"\xe9", "owned_member_lifecycle_valid")

    mark("owned_member_lifecycle_rollback")
    code += b"\x66\x83\xb8\x54\x0d\x00\x00\x02"
    branch(b"\x0f\x87", "owned_live_invalid")
    branch(b"\xe9", "owned_member_lifecycle_valid")

    mark("owned_member_lifecycle_settled")
    code += b"\x66\x83\xb8\x54\x0d\x00\x00\x02"
    branch(b"\x0f\x85", "owned_live_invalid")

    mark("owned_member_lifecycle_valid")
    code += b"\x48\x39\x90\x68\x0d\x00\x00"
    branch(b"\x0f\x85", "owned_live_invalid")
    code += b"\x48\x39\xf0"
    branch(b"\x0f\x85", "owned_list_next")
    code += b"\x41\xb8\x01\x00\x00\x00"
    code += b"\x44\x8b\x98\x2c\x0d\x00\x00"
    code += b"\x45\x85\xdb"
    branch(b"\x0f\x84", "owned_live_invalid")
    # Keep RAX as the traversal cursor.  Palette is scratch in R12D; loading
    # it into EAX would turn the following next-link read into ``palette+D70``.
    code += b"\x44\x8b\xa0\x7c\x0d\x00\x00"
    code += b"\x41\x83\xfc\x06"
    branch(b"\x0f\x87", "owned_live_invalid")
    code += b"\x45\x85\xd2"
    branch(b"\x0f\x84", "owned_compare_publication")
    code += b"\x41\x83\xfa\x05"
    branch(b"\x0f\x84", "owned_list_next")
    code += b"\x41\x83\xfa\x03"
    branch(b"\x0f\x82", "owned_list_next")
    code += b"\x44\x3b\x5d" + bytes((TARGET_HANDLE_OFFSET,))
    branch(b"\x0f\x85", "owned_live_invalid")
    code += b"\x44\x3b\x65" + bytes((TARGET_DESIRED_PALETTE_OFFSET,))
    branch(b"\x0f\x85", "owned_live_invalid")
    # 0x271650 encodes palette P into command+0x20 as
    # resource[0x30] + (P << 38).  Validate the actual 64-bit command consumed
    # by rendering, not a synthetic marker or only node+0xD7C.
    code += b"\x4c\x8b\x9e\x00\x0d\x00\x00"
    code += b"\x4d\x8b\x5b\x30"
    code += b"\x49\xc1\xe4\x26"
    code += b"\x4d\x03\xdc"
    code += b"\x4c\x39\x9e\xd0\x0b\x00\x00"
    branch(b"\x0f\x85", "owned_live_invalid")
    branch(b"\xe9", "owned_list_next")

    mark("owned_compare_publication")
    code += b"\x44\x3b\x5d" + bytes((TARGET_EXPECTED_TARGET_OFFSET,))
    branch(b"\x0f\x85", "owned_live_invalid")
    code += b"\x44\x3b\x65" + bytes((TARGET_EXPECTED_PALETTE_OFFSET,))
    branch(b"\x0f\x85", "owned_live_invalid")

    mark("owned_list_next")
    code += b"\x48\x89\xc2"
    code += b"\x48\x8b\x80\x70\x0d\x00\x00"
    code += b"\xff\xc9"
    branch(b"\x0f\x85", "owned_list_loop")
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x85", "owned_live_invalid")

    mark("owned_list_done")
    code += b"\x45\x85\xc0"
    branch(b"\x0f\x84", "owned_live_invalid")
    code += b"\x48\xb8" + struct.pack("<Q", TARGET_UI_MANAGER_GLOBAL)
    code += b"\x48\x39\x18"
    branch(b"\x0f\x85", "owned_live_invalid")
    code += b"\x4c\x39\xab\xc0\x0f\x01\x00"
    branch(b"\x0f\x85", "owned_live_invalid")
    code += b"\x4c\x39\x8b\x00\x0f\x01\x00"
    branch(b"\x0f\x85", "owned_live_invalid")
    code += b"\x41\x83\xfa\x05"
    branch(b"\x0f\x84", "owned_final_lifecycle_rollback")
    code += b"\x41\x83\xfa\x02"
    branch(b"\x0f\x82", "owned_final_lifecycle_settled")
    code += b"\x41\x83\xfa\x03"
    branch(b"\x0f\x87", "owned_final_lifecycle_settled")
    code += b"\x66\x83\xbe\x54\x0d\x00\x00\x01"
    branch(b"\x0f\x85", "owned_live_invalid")
    branch(b"\xe9", "owned_final_lifecycle_valid")

    mark("owned_final_lifecycle_rollback")
    code += b"\x66\x83\xbe\x54\x0d\x00\x00\x02"
    branch(b"\x0f\x87", "owned_live_invalid")
    branch(b"\xe9", "owned_final_lifecycle_valid")

    mark("owned_final_lifecycle_settled")
    code += b"\x66\x83\xbe\x54\x0d\x00\x00\x02"
    branch(b"\x0f\x85", "owned_live_invalid")

    mark("owned_final_lifecycle_valid")
    code += b"\x48\x83\xec\x28"
    call("validate_ownership_record")
    code += b"\x48\x83\xc4\x28"
    code += b"\x85\xc0"
    branch(b"\x0f\x84", "owned_live_invalid")
    code += b"\xb8\x01\x00\x00\x00\xc3"

    mark("owned_live_invalid")
    code += b"\x31\xc0\xc3"

    # Reconcile the resident record with the exact live node and publish both
    # observed fields into the terminal mailbox.  Caller has just completed a
    # successful validate_owned_live on the game thread.
    mark("publish_live_ownership")
    code += b"\x8b\x96\x2c\x0d\x00\x00"
    code += b"\x85\xd2"
    branch(b"\x0f\x84", "publish_live_invalid")
    code += b"\x8b\x8e\x7c\x0d\x00\x00"
    code += b"\x83\xf9\x06"
    branch(b"\x0f\x87", "publish_live_invalid")
    code += b"\x41\x89\x57\x20\x41\x89\x4f\x24"
    code += b"\x89\x55" + bytes((TARGET_OBSERVED_OFFSET,))
    code += b"\x89\x4d" + bytes((TARGET_OBSERVED_PALETTE_OFFSET,))
    publish_ownership_checksum()
    code += b"\xb8\x01\x00\x00\x00\xc3"

    mark("publish_live_invalid")
    code += b"\x31\xc0\xc3"

    # apply_palette: RSI is one fully authenticated node and RBP is the
    # in-flight mailbox.  The helper is an ABI-correct private caller with its
    # own shadow space; it updates D7C and the packed render command together.
    mark("apply_palette")
    code += b"\x8b\x45" + bytes((TARGET_DESIRED_PALETTE_OFFSET,))
    code += b"\x89\x86\x7c\x0d\x00\x00"
    code += b"\x44\x8b\xc8"
    code += b"\x48\x8b\x96\x00\x0d\x00\x00"
    code += b"\x48\x8d\x8e\xb0\x0b\x00\x00"
    code += b"\x45\x33\xc0"
    code += b"\x48\x83\xec\x28"
    code += b"\xc7\x44\x24\x20\x01\x00\x00\x00"
    code += b"\x48\xb8" + struct.pack("<Q", TARGET_CIRCLE_PALETTE_COMMAND)
    code += b"\xff\xd0"
    code += b"\x48\x83\xc4\x28\xc3"

    # copy_bytes(rcx=destination, rdx=source, r8d=size) ---------------------
    # Internal-only leaf used for the stack rollback image.  It preserves the
    # callback's nonvolatile node/manager registers and performs no calls.
    mark("copy_bytes")
    code += b"\x56\x57\x48\x8b\xf2\x48\x8b\xf9\x49\x8b\xc8"
    code += b"\xf3\xa4\x5f\x5e\xc3"

    return _finalize_internal_branches(code, int(cave), labels, fixups)


def build_retarget_dispatcher(cave: int, state: int) -> bytes:
    # Compatibility for callers/tests that previously selected a one-shot
    # retarget body.  The resident callback handles both operations.
    return build_target_dispatcher(cave, state)


def allocate_region(process: coop.RemoteProcess) -> int:
    base = process.ensure_damage_number_theme_allocation()
    return base + coop.NATIVE_TARGET_ROUTER_WORKSPACE_OFFSET


class NativeTargetCircleRetargeter:
    def __init__(self, process: coop.RemoteProcess):
        self.process = process
        self.region = allocate_region(process)
        self.cave = self.region + CODE_OFFSET
        self.state = self.region + STATE_OFFSET
        self.service = self.region + SERVICE_OFFSET
        self.palette_command_hook = (
            self.service + TARGET_PALETTE_COMMAND_HOOK_OFFSET
        )
        self.palette_field_hook = self.service + TARGET_PALETTE_FIELD_HOOK_OFFSET
        self.palette_create_hook = self.service + TARGET_PALETTE_CREATE_HOOK_OFFSET
        self._palette_root_contracts = (
            (
                TARGET_PALETTE_COMMAND_LOAD_SITE,
                TARGET_PALETTE_COMMAND_LOAD_ORIGINAL,
                b"\xe8"
                + _rel32(
                    TARGET_PALETTE_COMMAND_LOAD_SITE,
                    5,
                    self.palette_command_hook,
                ),
            ),
            (
                TARGET_PALETTE_FIELD_LOAD_SITE,
                TARGET_PALETTE_FIELD_LOAD_ORIGINAL,
                b"\xe8"
                + _rel32(
                    TARGET_PALETTE_FIELD_LOAD_SITE,
                    5,
                    self.palette_field_hook,
                ),
            ),
            (
                TARGET_PALETTE_CREATE_LOAD_SITE,
                TARGET_PALETTE_CREATE_LOAD_ORIGINAL,
                b"\xe8"
                + _rel32(
                    TARGET_PALETTE_CREATE_LOAD_SITE,
                    5,
                    self.palette_create_hook,
                ),
            ),
        )
        self.uses_shared_cave = True
        self.patched = False
        self.quarantined = False
        self.owns_static = False
        self._dispatch_code = build_target_dispatcher(self.cave, self.state)
        self._dispatch_patch: bytes | None = None
        self._sequence = 0
        damage_base = self.region - coop.NATIVE_TARGET_ROUTER_WORKSPACE_OFFSET
        self.mux = ui_tick_mux.NativeUITickMux(process, damage_base)
        self._install_handler()

    def _static_image(self) -> bytes:
        capacity = STATE_OFFSET - SERVICE_OFFSET
        image = bytearray(capacity)
        struct.pack_into(
            "<8sIIII",
            image,
            0,
            REGION_MAGIC,
            7,
            SERVICE_OFFSET,
            CODE_OFFSET,
            STATE_OFFSET,
        )
        hooks = build_target_palette_hooks(self.service, self.state)
        image[: len(hooks)] = hooks
        struct.pack_into(
            "<8sIIII",
            image,
            0,
            REGION_MAGIC,
            7,
            SERVICE_OFFSET,
            CODE_OFFSET,
            STATE_OFFSET,
        )
        code_offset = CODE_OFFSET - SERVICE_OFFSET
        if code_offset + len(self._dispatch_code) > capacity:
            raise RuntimeError(
                "target-circle callback exceeds its immutable reservation"
            )
        image[code_offset : code_offset + len(self._dispatch_code)] = (
            self._dispatch_code
        )
        selector = build_target_palette_selector(
            self.service + TARGET_PALETTE_SELECTOR_OFFSET,
            self.state,
        )
        selector_end = TARGET_PALETTE_SELECTOR_OFFSET + len(selector)
        if (
            code_offset + len(self._dispatch_code) > TARGET_PALETTE_SELECTOR_OFFSET
            or selector_end > capacity
        ):
            raise RuntimeError(
                "target-circle palette selector overlaps its immutable reservation"
            )
        image[TARGET_PALETTE_SELECTOR_OFFSET:selector_end] = selector
        return bytes(image)

    @staticmethod
    def _valid_state(image: bytes) -> bool:
        if (
            len(image) != STATE_SIZE
            or image[
                TARGET_STATE_MAGIC_OFFSET : TARGET_STATE_MAGIC_OFFSET
                + len(TARGET_STATE_MAGIC)
            ]
            != TARGET_STATE_MAGIC
            or any(
                image[
                    TARGET_STATE_MAGIC_OFFSET
                    + len(TARGET_STATE_MAGIC) : TARGET_OWNERSHIP_MAGIC_OFFSET
                ]
            )
            or image[
                TARGET_OWNERSHIP_MAGIC_OFFSET : TARGET_OWNERSHIP_MAGIC_OFFSET
                + len(TARGET_OWNERSHIP_MAGIC)
            ]
            != TARGET_OWNERSHIP_MAGIC
            or struct.unpack_from("<I", image, TARGET_OWNERSHIP_MAGIC_OFFSET + 0x08)[0]
            != TARGET_OWNERSHIP_VERSION
            or struct.unpack_from("<I", image, TARGET_OWNERSHIP_MAGIC_OFFSET + 0x0C)[0]
            != TARGET_OWNERSHIP_RECORD_COUNT
            or not struct.unpack_from("<I", image, TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET)[
                0
            ]
            or any(
                image[
                    TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET
                    + 4 : TARGET_OWNERSHIP_RECORDS_OFFSET
                ]
            )
            or any(image[TARGET_OWNERSHIP_RECORDS_END:])
        ):
            return False
        try:
            publications = [
                _unpack_ownership_record(image, slot)
                for slot in range(TARGET_OWNERSHIP_RECORD_COUNT)
            ]
        except (ValueError, struct.error):
            return False
        owned_nodes = [
            publication.node for publication in publications if publication is not None
        ]
        if len(owned_nodes) != len(set(owned_nodes)):
            return False
        owned_tokens = [
            publication.token for publication in publications if publication is not None
        ]
        next_token = struct.unpack_from(
            "<I", image, TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET
        )[0]
        if len(owned_tokens) != len(set(owned_tokens)) or next_token in owned_tokens:
            return False
        status = struct.unpack_from("<I", image, 0)[0]
        if status not in (0, 1, 2, 3, 4):
            return False
        sequence = struct.unpack_from("<I", image, TARGET_SEQUENCE_OFFSET)[0]
        ack = struct.unpack_from("<I", image, TARGET_ACK_SEQUENCE_OFFSET)[0]
        if status:
            operation = struct.unpack_from("<I", image, TARGET_OPERATION_OFFSET)[0]
            if operation not in (
                TARGET_OPERATION_CREATE,
                TARGET_OPERATION_RETARGET,
                TARGET_OPERATION_RETIRE,
                TARGET_OPERATION_RECONCILE,
            ):
                return False
            if not sequence:
                return False
            owner_tag = struct.unpack_from("<I", image, TARGET_OWNER_OFFSET)[0]
            auth = struct.unpack_from("<I", image, TARGET_AUTH_TOKEN_OFFSET)[0]
            observed = struct.unpack_from("<I", image, TARGET_OBSERVED_OFFSET)[0]
            observed_palette = struct.unpack_from(
                "<I", image, TARGET_OBSERVED_PALETTE_OFFSET
            )[0]
            desired_palette = struct.unpack_from(
                "<I", image, TARGET_DESIRED_PALETTE_OFFSET
            )[0]
            request_reserved = struct.unpack_from(
                "<I", image, TARGET_REQUEST_RESERVED_OFFSET
            )[0]
            if request_reserved not in (0, TARGET_PALETTE_OVERRIDE_COOKIE):
                return False
            if request_reserved and not (
                status == 2
                and operation in (
                    TARGET_OPERATION_CREATE,
                    TARGET_OPERATION_RETARGET,
                )
            ):
                return False
            if operation == TARGET_OPERATION_CREATE:
                if (
                    owner_tag not in TARGET_OWNER_TAGS
                    or not struct.unpack_from("<I", image, TARGET_HANDLE_OFFSET)[0]
                    or auth not in (0, 1)
                    or not 0 <= desired_palette <= 6
                ):
                    return False
                if status == 1 and (
                    auth
                    or any(image[TARGET_NODE_OFFSET : TARGET_HANDLE_OFFSET])
                    or observed
                    or observed_palette
                    or any(
                        image[
                            TARGET_RESULT_MANAGER_OFFSET : TARGET_ACK_SEQUENCE_OFFSET
                        ]
                    )
                ):
                    return False
            else:
                node = struct.unpack_from("<Q", image, TARGET_NODE_OFFSET)[0]
                manager = struct.unpack_from(
                    "<Q", image, TARGET_EXPECTED_MANAGER_OFFSET
                )[0]
                allocator = struct.unpack_from(
                    "<Q", image, TARGET_EXPECTED_ALLOCATOR_OFFSET
                )[0]
                expected_target = struct.unpack_from(
                    "<I", image, TARGET_EXPECTED_TARGET_OFFSET
                )[0]
                expected_palette = struct.unpack_from(
                    "<I", image, TARGET_EXPECTED_PALETTE_OFFSET
                )[0]
                if (
                    owner_tag not in TARGET_OWNER_TAGS
                    or not auth
                    or not node
                    or not manager
                    or not allocator
                    or not expected_target
                    or not 0 <= expected_palette <= 6
                ):
                    return False
                if operation == TARGET_OPERATION_RETARGET:
                    if (
                        not struct.unpack_from("<I", image, TARGET_HANDLE_OFFSET)[0]
                        or struct.unpack_from(
                            "<I", image, TARGET_VARIANT_OFFSET
                        )[0]
                        not in (1, 2)
                        or not 0 <= desired_palette <= 6
                    ):
                        return False
                elif (
                    struct.unpack_from("<I", image, TARGET_HANDLE_OFFSET)[0]
                    or struct.unpack_from("<I", image, TARGET_VARIANT_OFFSET)[0]
                    or desired_palette
                ):
                    return False
                if status == 1 and (observed or observed_palette):
                    return False
                if status == 3 and (
                    not observed or not 0 <= observed_palette <= 6
                ):
                    return False
                if status == 4 and (observed or observed_palette):
                    return False
        if status == 1 and ack != 0:
            return False
        if status == 2 and ack not in (0, sequence):
            return False
        if status in (3, 4) and ack != sequence:
            return False
        if status == 0 and ack not in (0, sequence):
            return False
        if status in (3, 4) and struct.unpack_from(
            "<I", image, TARGET_OPERATION_OFFSET
        )[0] == TARGET_OPERATION_CREATE:
            try:
                _unpack_create_journal(image)
            except (ValueError, struct.error):
                return False
        return True

    @staticmethod
    def _initial_state_image() -> bytes:
        image = bytearray(STATE_SIZE)
        image[
            TARGET_STATE_MAGIC_OFFSET : TARGET_STATE_MAGIC_OFFSET
            + len(TARGET_STATE_MAGIC)
        ] = TARGET_STATE_MAGIC
        struct.pack_into(
            "<8sII",
            image,
            TARGET_OWNERSHIP_MAGIC_OFFSET,
            TARGET_OWNERSHIP_MAGIC,
            TARGET_OWNERSHIP_VERSION,
            TARGET_OWNERSHIP_RECORD_COUNT,
        )
        struct.pack_into(
            "<I",
            image,
            TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET,
            1,
        )
        return bytes(image)

    def _install_handler(self) -> None:
        with _SERVICE_LOCK:
            self._install_handler_locked()

    def _install_handler_locked(self) -> None:
        expected = self._static_image()
        current = self.process.read(self.region + SERVICE_OFFSET, len(expected))
        state_image = self.process.read(self.state, STATE_SIZE)
        slot_address = self.mux.slot_address(ui_tick_mux.SLOT_TARGET_CIRCLE)
        slot = self.process.read(slot_address, 8)
        expected_slot = struct.pack("<Q", self.cave)
        root_images = tuple(
            self.process.read(site, len(original))
            for site, original, _patch in self._palette_root_contracts
        )
        roots_known = all(
            current_root in (original, patch)
            for current_root, (_site, original, patch) in zip(
                root_images,
                self._palette_root_contracts,
            )
        )
        roots_published = all(
            current_root == patch
            for current_root, (_site, _original, patch) in zip(
                root_images,
                self._palette_root_contracts,
            )
        )

        initial_state = self._initial_state_image()
        fresh = bool(
            current == b"\x00" * len(expected)
            and state_image == b"\x00" * STATE_SIZE
            and slot == b"\x00" * 8
            and all(
                current_root == original
                for current_root, (_site, original, _patch) in zip(
                    root_images,
                    self._palette_root_contracts,
                )
            )
        )
        staged = bool(
            current == expected
            and state_image == b"\x00" * STATE_SIZE
            and slot == b"\x00" * 8
        )
        prepared = bool(
            current == expected and state_image == initial_state and slot == b"\x00" * 8
        )
        adopted = bool(
            current == expected
            and slot == expected_slot
            and self._valid_state(state_image)
            and roots_published
        )
        if not roots_known or not (fresh or staged or prepared or adopted):
            raise RuntimeError(
                "native target-circle callback topology is partial or foreign; "
                "restart the game"
            )

        # Local callback, mailbox and slot are proven before the broker is
        # allowed to publish its permanent root.  A foreign service image must
        # therefore cause literally zero root/cave mutation.
        self.mux.ensure_installed()

        if fresh:
            try:
                self.process.patch_code(
                    self.region + SERVICE_OFFSET,
                    expected,
                )
            except BaseException as error:
                raise RuntimeError(
                    "native target-circle callback preparation failed; restart the game"
                ) from error
            self.owns_static = True
            if (
                self.process.read(self.region + SERVICE_OFFSET, len(expected))
                != expected
            ):
                raise RuntimeError(
                    "native target-circle callback preparation did not persist; "
                    "restart the game"
                )

        if fresh or staged:
            try:
                self.process.write(self.state, initial_state)
            except BaseException as error:
                raise RuntimeError(
                    "native target-circle mailbox preparation failed; restart the game"
                ) from error
            self.owns_static = True
            if self.process.read(self.state, STATE_SIZE) != initial_state:
                raise RuntimeError(
                    "native target-circle mailbox preparation did not persist; "
                    "restart the game"
                )

        if not adopted:
            if not (
                self.process.read(
                    self.region + SERVICE_OFFSET,
                    len(expected),
                )
                == expected
                and self.process.read(self.state, STATE_SIZE) == initial_state
                and self.process.read(slot_address, 8) == b"\x00" * 8
            ):
                raise RuntimeError(
                    "native target-circle palette topology changed before hook "
                    "publication; restart the game"
                )
            for site, original, patch in self._palette_root_contracts:
                current_root = self.process.read(site, len(original))
                if current_root == patch:
                    continue
                if current_root != original:
                    raise RuntimeError(
                        "native target-circle palette root became foreign; "
                        "restart the game"
                    )
                try:
                    self.process.patch_code(site, patch)
                except BaseException as error:
                    raise RuntimeError(
                        "native target-circle palette hook publication failed; "
                        "restart the game"
                    ) from error
                if self.process.read(site, len(patch)) != patch:
                    raise RuntimeError(
                        "native target-circle palette hook did not persist; "
                        "restart the game"
                    )
                self.owns_static = True

        if not adopted:
            if not (
                self.mux.resident_exact()
                and self.process.read(
                    self.region + SERVICE_OFFSET,
                    len(expected),
                )
                == expected
                and self.process.read(self.state, STATE_SIZE) == initial_state
                and self.process.read(slot_address, 8) == b"\x00" * 8
                and all(
                    self.process.read(site, len(patch)) == patch
                    for site, _original, patch in self._palette_root_contracts
                )
            ):
                raise RuntimeError(
                    "native target-circle topology changed before callback "
                    "publication; restart the game"
                )
            try:
                self.mux.register_handler(ui_tick_mux.SLOT_TARGET_CIRCLE, self.cave)
            except BaseException as error:
                raise RuntimeError(
                    "native target-circle callback publication failed; restart the game"
                ) from error
            self.owns_static = True

        if not self.resident_exact():
            raise RuntimeError(
                "native target-circle callback publication could not be verified; "
                "restart the game"
            )

        self._dispatch_patch = self.mux.patch
        state_image = self.process.read(self.state, STATE_SIZE)
        if not self._valid_state(state_image):
            raise RuntimeError(
                "native target-circle mailbox is invalid; restart the game"
            )
        self._sequence = struct.unpack_from("<I", state_image, TARGET_SEQUENCE_OFFSET)[
            0
        ]
        status = struct.unpack_from("<I", state_image, 0)[0]
        self.patched = status in (1, 2)
        self.quarantined = self.patched

    def resident_exact(self) -> bool:
        try:
            expected = self._static_image()
            return bool(
                self.mux.handler_registered(
                    ui_tick_mux.SLOT_TARGET_CIRCLE,
                    self.cave,
                )
                and self.process.read(
                    self.region + SERVICE_OFFSET,
                    len(expected),
                )
                == expected
                and all(
                    self.process.read(site, len(patch)) == patch
                    for site, _original, patch in self._palette_root_contracts
                )
            )
        except OSError:
            return False

    def maintain(self) -> bool:
        """Poll only mutable state; resident code and root are never reclaimed."""

        if not self.resident_exact():
            self.quarantined = True
            return False
        try:
            state_image = self.process.read(self.state, STATE_SIZE)
        except OSError:
            self.quarantined = True
            return False
        if not self._valid_state(state_image):
            self.quarantined = True
            return False
        dispatch_state = struct.unpack_from("<I", state_image)[0]
        if dispatch_state in (1, 2):
            self.patched = True
            self.quarantined = True
            return False
        if dispatch_state not in (0, 3, 4):
            self.quarantined = True
            return False
        self.patched = False
        self.quarantined = False
        return True

    def _ownerships_locked(self) -> tuple[TargetCircleOwnership | None, ...]:
        state_image = self.process.read(self.state, STATE_SIZE)
        if not self._valid_state(state_image):
            raise RuntimeError("native target-circle ownership registry is invalid")
        return tuple(
            _unpack_ownership_record(state_image, slot)
            for slot in range(TARGET_OWNERSHIP_RECORD_COUNT)
        )

    def _write_ownership_locked(
        self,
        slot: int,
        ownership: TargetCircleOwnership | None,
    ) -> None:
        payload = (
            b"\x00" * TARGET_OWNERSHIP_RECORD_SIZE
            if ownership is None
            else _pack_ownership_record(ownership)
        )
        address = (
            self.state
            + TARGET_OWNERSHIP_RECORDS_OFFSET
            + int(slot) * TARGET_OWNERSHIP_RECORD_SIZE
        )
        self.process.write(address, payload)
        observed = self._ownerships_locked()[int(slot)]
        if observed != ownership:
            raise RuntimeError("target-circle ownership publication did not persist")

    def _consume_ownership_token_locked(
        self,
        publications: tuple[TargetCircleOwnership | None, ...],
    ) -> int:
        state_image = self.process.read(self.state, STATE_SIZE)
        if not self._valid_state(state_image):
            raise RuntimeError("native target-circle ownership registry is invalid")
        token = struct.unpack_from(
            "<I", state_image, TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET
        )[0]
        used_tokens = {
            publication.token for publication in publications if publication is not None
        }
        for _ in range(len(used_tokens) + 1):
            if token not in used_tokens:
                break
            token = (token + 1) & 0xFFFFFFFF
            token = token or 1
        else:
            raise RuntimeError("target-circle ownership token space is exhausted")
        next_token = (token + 1) & 0xFFFFFFFF
        next_token = next_token or 1
        while next_token in used_tokens:
            next_token = (next_token + 1) & 0xFFFFFFFF
            next_token = next_token or 1
        # Advance and verify the generation before publishing the record.  A
        # crash can skip a token, but can never make a stale controller's token
        # current again (the practical ABA failure mode).
        self.process.write(
            self.state + TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET,
            struct.pack("<I", next_token),
        )
        observed = self.process.u32(self.state + TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET)
        if observed != next_token:
            raise RuntimeError("target-circle ownership generation did not persist")
        return token

    def ownership(self, owner_tag: int) -> TargetCircleOwnership | None:
        slot = _owner_slot(owner_tag)
        with _SERVICE_LOCK:
            if not self.maintain():
                raise RuntimeError("native target-circle service is unavailable")
            return self._ownerships_locked()[slot]

    def pending_creation(self) -> TargetCircleCreateJournal | None:
        """Read the one shared, unconsumed terminal CREATE transaction."""

        with _SERVICE_LOCK:
            if not self.maintain():
                return None
            state_image = self.process.read(self.state, STATE_SIZE)
            if not self._valid_state(state_image):
                raise RuntimeError("native target-circle CREATE journal is invalid")
            return _unpack_create_journal(state_image)

    def consume_creation(self, journal: TargetCircleCreateJournal) -> bool:
        """Consume an exact terminal CREATE only after its durable resolution.

        Successful CREATE results require a matching resident ownership record.
        A rejection is consumable only when the callback proves that the native
        factory was never entered.  Once entered, an uncertain allocation stays
        process-resident and blocks every later request rather than exhausting
        the game's fixed UI pool.
        """

        with _SERVICE_LOCK:
            if not self.maintain():
                return False
            state_image = self.process.read(self.state, STATE_SIZE)
            if not self._valid_state(state_image):
                return False
            current = _unpack_create_journal(state_image)
            if current != journal:
                return False
            if current.status == 3:
                publication = self._ownerships_locked()[
                    _owner_slot(current.owner_tag)
                ]
                if publication is None or not (
                    publication.owner_tag == current.owner_tag
                    and publication.manager == current.manager
                    and publication.allocator == current.allocator
                    and publication.node == current.node
                ):
                    return False
                if not self._live_node_locked(
                    manager=publication.manager,
                    allocator=publication.allocator,
                    node=publication.node,
                    target=publication.target,
                    palette=publication.palette,
                ):
                    return False
            elif current.factory_entered:
                return False
            try:
                self.process.write(self.state, b"\x00" * 4)
            except OSError:
                return False
            observed = self.process.read(self.state, STATE_SIZE)
            return bool(
                self._valid_state(observed)
                and struct.unpack_from("<I", observed, 0)[0] == 0
                and _unpack_create_journal(observed) is None
            )

    def discard_creation_from_obsolete_allocator(
        self,
        journal: TargetCircleCreateJournal,
        *,
        manager: int,
        allocator: int,
    ) -> bool:
        """Neutralize an old CREATE only after a stable, different heap exists.

        A nonzero allocator identity different from the callback-journaled heap
        is the same generation proof used to retire obsolete ownership records.
        The journal remains the circuit breaker for zero, equal or changing
        identities.  If a crash happens after clearing an exact old sidecar but
        before status zero, the still-resident journal continues to block CREATE.
        """

        manager = int(manager) & 0xFFFFFFFFFFFFFFFF
        allocator = int(allocator) & 0xFFFFFFFFFFFFFFFF
        if (
            not manager
            or not allocator
            or not journal.allocator
            or allocator == journal.allocator
        ):
            return False

        def generation_is_stable() -> bool:
            return bool(
                self.process.u64(TARGET_UI_MANAGER_GLOBAL) == manager
                and self.process.u64(manager + TARGET_CIRCLE_ALLOCATOR_OFFSET)
                == allocator
                and self.process.u64(TARGET_UI_MANAGER_GLOBAL) == manager
                and self.process.u64(manager + TARGET_CIRCLE_ALLOCATOR_OFFSET)
                == allocator
            )

        with _SERVICE_LOCK:
            if not self.maintain() or not generation_is_stable():
                return False
            state_image = self.process.read(self.state, STATE_SIZE)
            if not self._valid_state(state_image):
                return False
            current = _unpack_create_journal(state_image)
            if current != journal:
                return False
            publications = self._ownerships_locked()
            slot = _owner_slot(journal.owner_tag)
            publication = publications[slot]
            if publication is not None:
                if not (
                    publication.owner_tag == journal.owner_tag
                    and publication.manager == journal.manager
                    and publication.allocator == journal.allocator
                    and publication.node == journal.node
                ):
                    return False
                self._write_ownership_locked(slot, None)
            # Re-prove both the transaction and the new generation after the
            # sidecar mutation.  A transition back to the old heap leaves the
            # journal intact and therefore cannot authorize a duplicate CREATE.
            if not generation_is_stable():
                return False
            state_image = self.process.read(self.state, STATE_SIZE)
            if (
                not self._valid_state(state_image)
                or _unpack_create_journal(state_image) != journal
            ):
                return False
            try:
                self.process.write(self.state, b"\x00" * 4)
            except OSError:
                return False
            observed = self.process.read(self.state, STATE_SIZE)
            return bool(
                generation_is_stable()
                and self._valid_state(observed)
                and struct.unpack_from("<I", observed, 0)[0] == 0
                and _unpack_create_journal(observed) is None
            )

    def discard_failed_creation_from_quiescent_node(
        self,
        journal: TargetCircleCreateJournal,
    ) -> bool:
        """Recover a rejected CREATE that names one proven inactive owned node.

        The native UI can unlink and deactivate a circle before Python observes
        the menu-close edge. A following owner's CREATE then enters the factory,
        is handed that inactive pool node and returns rejected. The terminal
        journal and the old ownership record otherwise block every circle for
        the rest of the process.

        Recovery is allowed only for the exact state seen in that lifecycle:
        the current manager/allocator still match the journal, both native list
        roots are empty, the journaled node is state 0 with null links, and one
        authenticated ownership record describes that same node and its still
        intact target/palette fields. Any partial or live identity remains
        fail-closed.
        """

        if journal.status != 4 or not journal.factory_entered:
            return False

        def quiescent(publication: TargetCircleOwnership) -> bool:
            return bool(
                self.process.u64(TARGET_UI_MANAGER_GLOBAL) == journal.manager
                and self.process.u64(
                    journal.manager + TARGET_CIRCLE_ALLOCATOR_OFFSET
                )
                == journal.allocator
                and self.process.u64(journal.manager + 0x10F00) == 0
                and self.process.u64(journal.manager + 0x10EF0) == 0
                and struct.unpack(
                    "<H", self.process.read(journal.node + 0xD54, 2)
                )[0]
                == 0
                and self.process.u64(journal.node + 0xD68) == 0
                and self.process.u64(journal.node + 0xD70) == 0
                and self.process.u32(journal.node + 0xD2C) == publication.target
                and self.process.u32(journal.node + 0xD7C) == publication.palette
                and self.process.u64(TARGET_UI_MANAGER_GLOBAL) == journal.manager
                and self.process.u64(
                    journal.manager + TARGET_CIRCLE_ALLOCATOR_OFFSET
                )
                == journal.allocator
                and self.process.u64(journal.manager + 0x10F00) == 0
                and self.process.u64(journal.manager + 0x10EF0) == 0
            )

        with _SERVICE_LOCK:
            if not self.maintain():
                return False
            state_image = self.process.read(self.state, STATE_SIZE)
            if (
                not self._valid_state(state_image)
                or _unpack_create_journal(state_image) != journal
            ):
                return False
            publications = self._ownerships_locked()
            matches = tuple(
                (slot, publication)
                for slot, publication in enumerate(publications)
                if publication is not None
                and publication.manager == journal.manager
                and publication.allocator == journal.allocator
                and publication.node == journal.node
            )
            if len(matches) != 1:
                return False
            slot, publication = matches[0]
            if not quiescent(publication):
                return False

            self._write_ownership_locked(slot, None)
            state_image = self.process.read(self.state, STATE_SIZE)
            if (
                not quiescent(publication)
                or not self._valid_state(state_image)
                or _unpack_create_journal(state_image) != journal
            ):
                return False
            try:
                self.process.write(self.state, b"\x00" * 4)
            except OSError:
                return False
            observed = self.process.read(self.state, STATE_SIZE)
            return bool(
                self._valid_state(observed)
                and struct.unpack_from("<I", observed, 0)[0] == 0
                and _unpack_create_journal(observed) is None
            )

    def release_quiescent_ownership(
        self,
        ownership: TargetCircleOwnership,
    ) -> bool:
        """Release an exact inactive node while no transaction or list exists."""

        slot = _owner_slot(ownership.owner_tag)

        def quiescent() -> bool:
            return bool(
                self.process.u64(TARGET_UI_MANAGER_GLOBAL) == ownership.manager
                and self.process.u64(
                    ownership.manager + TARGET_CIRCLE_ALLOCATOR_OFFSET
                )
                == ownership.allocator
                and self.process.u64(ownership.manager + 0x10F00) == 0
                and self.process.u64(ownership.manager + 0x10EF0) == 0
                and struct.unpack(
                    "<H", self.process.read(ownership.node + 0xD54, 2)
                )[0]
                == 0
                and self.process.u64(ownership.node + 0xD68) == 0
                and self.process.u64(ownership.node + 0xD70) == 0
                and self.process.u32(ownership.node + 0xD2C) == ownership.target
                and self.process.u32(ownership.node + 0xD7C) == ownership.palette
                and self.process.u64(TARGET_UI_MANAGER_GLOBAL) == ownership.manager
                and self.process.u64(
                    ownership.manager + TARGET_CIRCLE_ALLOCATOR_OFFSET
                )
                == ownership.allocator
                and self.process.u64(ownership.manager + 0x10F00) == 0
                and self.process.u64(ownership.manager + 0x10EF0) == 0
            )

        with _SERVICE_LOCK:
            if not self.maintain():
                return False
            state_image = self.process.read(self.state, STATE_SIZE)
            if (
                not self._valid_state(state_image)
                or struct.unpack_from("<I", state_image, 0)[0] != 0
                or _unpack_create_journal(state_image) is not None
                or self._ownerships_locked()[slot] != ownership
                or not quiescent()
            ):
                return False
            self._write_ownership_locked(slot, None)
            state_image = self.process.read(self.state, STATE_SIZE)
            return bool(
                quiescent()
                and self._valid_state(state_image)
                and struct.unpack_from("<I", state_image, 0)[0] == 0
                and _unpack_create_journal(state_image) is None
                and self._ownerships_locked()[slot] is None
            )

    def release_frontend_reset_ownership(
        self,
        ownership: TargetCircleOwnership,
    ) -> bool:
        """Forget one node only after the native frontend emptied its pool view.

        Fullscreen native menus can reset an inactive target-circle pool block,
        including its links, target and palette, without replacing the allocator.
        In that state the old sidecar is no longer a usable node identity.  This
        proof intentionally ignores reset block contents: it requires the exact
        authenticated publication, an idle dispatcher, stable empty list roots
        and lifecycle zero before and after the sidecar-only write.
        """

        slot = _owner_slot(ownership.owner_tag)

        def frontend_reset_is_stable() -> bool:
            return bool(
                self.process.u64(TARGET_UI_MANAGER_GLOBAL) == ownership.manager
                and self.process.u64(
                    ownership.manager + TARGET_CIRCLE_ALLOCATOR_OFFSET
                )
                == ownership.allocator
                and self.process.u64(ownership.manager + 0x10F00) == 0
                and self.process.u64(ownership.manager + 0x10EF0) == 0
                and struct.unpack(
                    "<H", self.process.read(ownership.node + 0xD54, 2)
                )[0]
                == 0
                and self.process.u64(TARGET_UI_MANAGER_GLOBAL) == ownership.manager
                and self.process.u64(
                    ownership.manager + TARGET_CIRCLE_ALLOCATOR_OFFSET
                )
                == ownership.allocator
                and self.process.u64(ownership.manager + 0x10F00) == 0
                and self.process.u64(ownership.manager + 0x10EF0) == 0
                and struct.unpack(
                    "<H", self.process.read(ownership.node + 0xD54, 2)
                )[0]
                == 0
            )

        with _SERVICE_LOCK:
            if not self.maintain():
                return False
            state_image = self.process.read(self.state, STATE_SIZE)
            dispatch_state = struct.unpack_from("<I", state_image, 0)[0]
            if (
                not self._valid_state(state_image)
                or dispatch_state in (1, 2)
                or _unpack_create_journal(state_image) is not None
                or self._ownerships_locked()[slot] != ownership
                or not frontend_reset_is_stable()
            ):
                return False
            self._write_ownership_locked(slot, None)
            state_image = self.process.read(self.state, STATE_SIZE)
            return bool(
                frontend_reset_is_stable()
                and self._valid_state(state_image)
                and struct.unpack_from("<I", state_image, 0)[0] not in (1, 2)
                and _unpack_create_journal(state_image) is None
                and self._ownerships_locked()[slot] is None
            )

    def _live_node_locked(
        self,
        *,
        manager: int,
        allocator: int,
        node: int,
        target: int,
        palette: int,
    ) -> bool:
        """Prove one exact settled list member in one heap generation."""

        if (
            not manager
            or not allocator
            or not node
            or self.process.u64(TARGET_UI_MANAGER_GLOBAL) != manager
            or self.process.u64(manager + TARGET_CIRCLE_ALLOCATOR_OFFSET)
            != allocator
        ):
            return False
        head = self.process.u64(manager + 0x10F00)
        nodes: list[int] = []
        prev_links: list[int] = []
        links: list[int] = []
        seen: set[int] = set()
        current = head
        while current:
            if current in seen or len(nodes) >= 8:
                return False
            seen.add(current)
            nodes.append(current)
            previous = self.process.u64(current + 0xD68)
            following = self.process.u64(current + 0xD70)
            prev_links.append(previous)
            links.append(following)
            current = following
        if node not in seen or any(
            previous != (nodes[index - 1] if index else 0)
            for index, previous in enumerate(prev_links)
        ):
            return False
        if (
            struct.unpack("<H", self.process.read(node + 0xD54, 2))[0] != 2
            or self.process.u32(node + 0xD2C) != target
            or self.process.u32(node + 0xD7C) != palette
            or self.process.u64(TARGET_UI_MANAGER_GLOBAL) != manager
            or self.process.u64(manager + TARGET_CIRCLE_ALLOCATOR_OFFSET)
            != allocator
            or self.process.u64(manager + 0x10F00) != head
            or any(
                self.process.u64(item + 0xD68) != previous
                for item, previous in zip(nodes, prev_links)
            )
            or any(
                self.process.u64(item + 0xD70) != following
                for item, following in zip(nodes, links)
            )
        ):
            return False
        return True

    def claim_ownership(
        self,
        owner_tag: int,
        allocator: int,
        node: int,
        target: int,
        palette: int,
    ) -> TargetCircleOwnership:
        slot = _owner_slot(owner_tag)
        allocator = int(allocator) & 0xFFFFFFFFFFFFFFFF
        node = int(node) & 0xFFFFFFFFFFFFFFFF
        target = int(target) & 0xFFFFFFFF
        palette = int(palette)
        if not allocator or not node or not target or not 0 <= palette <= 6:
            raise ValueError("invalid target-circle ownership publication")
        with _SERVICE_LOCK:
            if not self.maintain():
                raise RuntimeError("native target-circle service is unavailable")
            state_image = self.process.read(self.state, STATE_SIZE)
            journal = _unpack_create_journal(state_image)
            if journal is None or not (
                journal.status == 3
                and journal.owner_tag == (int(owner_tag) & 0xFFFFFFFF)
                and journal.allocator == allocator
                and journal.node == node
                and journal.target == target
                and journal.observed == target
                and journal.palette == palette
            ):
                raise RuntimeError(
                    "target-circle ownership requires its exact successful CREATE journal"
                )
            publications = self._ownerships_locked()
            if publications[slot] is not None:
                raise RuntimeError("target-circle owner already has a publication")
            if any(
                publication is not None and publication.node == node
                for publication in publications
            ):
                raise RuntimeError("target-circle node is already explicitly owned")
            manager = self.process.u64(TARGET_UI_MANAGER_GLOBAL)
            if not manager or manager != journal.manager:
                raise RuntimeError("target-circle manager is unavailable")
            if not self._live_node_locked(
                manager=manager,
                allocator=allocator,
                node=node,
                target=target,
                palette=palette,
            ):
                raise RuntimeError(
                    "target-circle node is not an exact live member of this heap"
                )
            token = self._consume_ownership_token_locked(publications)
            ownership = TargetCircleOwnership(
                owner_tag=int(owner_tag) & 0xFFFFFFFF,
                token=token,
                manager=manager,
                allocator=allocator,
                node=node,
                target=target,
                palette=palette,
            )
            self._write_ownership_locked(slot, ownership)
            return ownership

    def update_ownership(
        self,
        ownership: TargetCircleOwnership,
        *,
        target: int,
        palette: int,
    ) -> TargetCircleOwnership:
        target = int(target) & 0xFFFFFFFF
        palette = int(palette)
        if not target or not 0 <= palette <= 6:
            raise ValueError("invalid target-circle ownership update")
        updated = self._execute_owned(
            ownership,
            operation=TARGET_OPERATION_RETARGET,
            target=target,
            palette=palette,
        )
        if updated is None:
            raise RuntimeError("target-circle UPDATE unexpectedly retired ownership")
        return updated

    def reconcile_ownership(
        self,
        ownership: TargetCircleOwnership,
    ) -> TargetCircleOwnership:
        updated = self._execute_owned(
            ownership,
            operation=TARGET_OPERATION_RECONCILE,
        )
        if updated is None:
            raise RuntimeError("target-circle RECONCILE unexpectedly retired ownership")
        return updated

    def release_ownership(self, ownership: TargetCircleOwnership) -> bool:
        slot = _owner_slot(ownership.owner_tag)
        with _SERVICE_LOCK:
            if not self.maintain():
                return False
            publications = self._ownerships_locked()
            if publications[slot] != ownership:
                return False
            self._write_ownership_locked(slot, None)
            return True

    def retire_ownership(self, ownership: TargetCircleOwnership) -> bool:
        """Retire only through the authenticated game-thread transaction."""

        try:
            result = self._execute_owned(
                ownership,
                operation=TARGET_OPERATION_RETIRE,
            )
        except (OSError, RuntimeError, struct.error, ValueError):
            return False
        return result is None

    def arm(self, payload: bytes | bytearray, code: bytes | None = None) -> int:
        """Publish one request without ever rewriting executable memory."""

        del code  # compatibility with the retired one-shot builder API
        with _SERVICE_LOCK:
            if not self.maintain():
                raise RuntimeError("target-circle dispatcher is still in flight")
            resident = self.process.read(self.state, STATE_SIZE)
            pending = _unpack_create_journal(resident)
            if pending is not None:
                raise RuntimeError(
                    "target-circle CREATE result is unconsumed "
                    f"(owner={pending.owner_tag:#x}, sequence={pending.sequence})"
                )
            payload = bytes(payload)
            if len(payload) > TARGET_STATE_MAGIC_OFFSET:
                raise RuntimeError(
                    "target-circle mailbox payload exceeds its declared fields "
                    f"({len(payload)} > {TARGET_STATE_MAGIC_OFFSET})"
                )
            requested_operation = (
                struct.unpack_from("<I", payload, TARGET_OPERATION_OFFSET)[0]
                if len(payload) >= TARGET_OPERATION_OFFSET + 4
                else 0
            )
            node = (
                struct.unpack_from("<Q", payload, TARGET_NODE_OFFSET)[0]
                if len(payload) >= TARGET_NODE_OFFSET + 8
                else 0
            )
            operation = requested_operation or (
                TARGET_OPERATION_RETARGET if node else TARGET_OPERATION_CREATE
            )
            owner_tag = (
                struct.unpack_from("<I", payload, TARGET_OWNER_OFFSET)[0]
                if len(payload) >= TARGET_OWNER_OFFSET + 4
                else 0
            )
            if operation == TARGET_OPERATION_CREATE:
                if owner_tag not in TARGET_OWNER_TAGS:
                    raise ValueError("target-circle CREATE requires an explicit owner")
                if self._ownerships_locked()[_owner_slot(owner_tag)] is not None:
                    raise RuntimeError(
                        "target-circle CREATE owner already has a publication"
                    )
                if (
                    any(
                        payload[
                            TARGET_FACTORY_ENTERED_OFFSET : TARGET_ACK_SEQUENCE_OFFSET
                        ]
                    )
                    or any(payload[TARGET_NODE_OFFSET:TARGET_HANDLE_OFFSET])
                    or any(payload[TARGET_OBSERVED_OFFSET:TARGET_SEQUENCE_OFFSET])
                    or struct.unpack_from(
                        "<I", payload, TARGET_OBSERVED_PALETTE_OFFSET
                    )[0]
                    or not 0
                    <= struct.unpack_from(
                        "<I", payload, TARGET_DESIRED_PALETTE_OFFSET
                    )[0]
                    <= 6
                    or struct.unpack_from(
                        "<I", payload, TARGET_REQUEST_RESERVED_OFFSET
                    )[0]
                ):
                    raise ValueError("target-circle CREATE result fields must be zero")
            elif operation in (
                TARGET_OPERATION_RETARGET,
                TARGET_OPERATION_RETIRE,
                TARGET_OPERATION_RECONCILE,
            ):
                if len(payload) < TARGET_REQUEST_RESERVED_OFFSET + 4:
                    raise ValueError(
                        "owned target-circle request is missing authentication fields"
                    )
                token = struct.unpack_from(
                    "<I", payload, TARGET_AUTH_TOKEN_OFFSET
                )[0]
                manager = struct.unpack_from(
                    "<Q", payload, TARGET_EXPECTED_MANAGER_OFFSET
                )[0]
                allocator = struct.unpack_from(
                    "<Q", payload, TARGET_EXPECTED_ALLOCATOR_OFFSET
                )[0]
                expected_target = struct.unpack_from(
                    "<I", payload, TARGET_EXPECTED_TARGET_OFFSET
                )[0]
                expected_palette = struct.unpack_from(
                    "<I", payload, TARGET_EXPECTED_PALETTE_OFFSET
                )[0]
                desired_palette = struct.unpack_from(
                    "<I", payload, TARGET_DESIRED_PALETTE_OFFSET
                )[0]
                if (
                    owner_tag not in TARGET_OWNER_TAGS
                    or not token
                    or not manager
                    or not allocator
                    or not node
                    or not expected_target
                    or not 0 <= expected_palette <= 6
                    or struct.unpack_from(
                        "<I", payload, TARGET_OBSERVED_OFFSET
                    )[0]
                    or struct.unpack_from(
                        "<I", payload, TARGET_OBSERVED_PALETTE_OFFSET
                    )[0]
                    or struct.unpack_from(
                        "<I", payload, TARGET_REQUEST_RESERVED_OFFSET
                    )[0]
                ):
                    raise ValueError("owned target-circle authentication is invalid")
                if operation == TARGET_OPERATION_RETARGET:
                    if (
                        not struct.unpack_from(
                            "<I", payload, TARGET_HANDLE_OFFSET
                        )[0]
                        or struct.unpack_from(
                            "<I", payload, TARGET_VARIANT_OFFSET
                        )[0]
                        not in (1, 2)
                        or not 0 <= desired_palette <= 6
                    ):
                        raise ValueError("target-circle UPDATE request is invalid")
                elif (
                    struct.unpack_from("<I", payload, TARGET_HANDLE_OFFSET)[0]
                    or struct.unpack_from("<I", payload, TARGET_VARIANT_OFFSET)[0]
                    or desired_palette
                ):
                    raise ValueError(
                        "target-circle RETIRE/RECONCILE carries mutation fields"
                    )
                expected = TargetCircleOwnership(
                    owner_tag=owner_tag,
                    token=token,
                    manager=manager,
                    allocator=allocator,
                    node=node,
                    target=expected_target,
                    palette=expected_palette,
                )
                publications = self._ownerships_locked()
                if publications[_owner_slot(owner_tag)] != expected:
                    raise RuntimeError("target-circle ownership token is stale")
            else:
                raise ValueError("unknown target-circle operation")
            # Router instances for P1/P2/P3 share this mailbox.  Derive the
            # generation from the resident publication while holding the
            # service lock; an instance-local counter is stale as soon as a
            # different player publishes a request.
            resident_sequence = self.process.u32(self.state + TARGET_SEQUENCE_OFFSET)
            sequence = (resident_sequence + 1) & 0xFFFFFFFF
            sequence = sequence or 1
            # Only the transient prefix is replaced.  Explicit ownership is a
            # separate persistent registry and survives requests/restarts.
            mailbox = bytearray(TARGET_STATE_MAGIC_OFFSET)
            mailbox[: len(payload)] = payload
            struct.pack_into("<I", mailbox, 0, 0)
            struct.pack_into("<I", mailbox, TARGET_OPERATION_OFFSET, operation)
            struct.pack_into("<I", mailbox, TARGET_SEQUENCE_OFFSET, sequence)
            struct.pack_into("<I", mailbox, TARGET_ACK_SEQUENCE_OFFSET, 0)

            self.patched = True
            self.quarantined = False
            try:
                self.process.write(self.state, mailbox)
                # State 1 is the only publication observed by the resident callback.
                self.process.write(self.state, struct.pack("<I", 1))
            except BaseException as error:
                self.quarantined = True
                raise RuntimeError(
                    "target-circle mailbox publication failed"
                ) from error
            self._sequence = sequence
            return sequence

    def _result_ready(self) -> bool:
        return (
            self.process.u32(self.state + TARGET_ACK_SEQUENCE_OFFSET) == self._sequence
        )

    @staticmethod
    def _owned_payload(
        ownership: TargetCircleOwnership,
        *,
        operation: int,
        target: int = 0,
        palette: int = 0,
    ) -> bytes:
        _owner_slot(ownership.owner_tag)
        if operation not in (
            TARGET_OPERATION_RETARGET,
            TARGET_OPERATION_RETIRE,
            TARGET_OPERATION_RECONCILE,
        ):
            raise ValueError("invalid owned target-circle operation")
        payload = bytearray(TARGET_STATE_MAGIC_OFFSET)
        struct.pack_into("<I", payload, TARGET_OPERATION_OFFSET, operation)
        struct.pack_into("<Q", payload, TARGET_NODE_OFFSET, ownership.node)
        struct.pack_into("<I", payload, TARGET_OWNER_OFFSET, ownership.owner_tag)
        struct.pack_into("<I", payload, TARGET_AUTH_TOKEN_OFFSET, ownership.token)
        struct.pack_into(
            "<Q", payload, TARGET_EXPECTED_MANAGER_OFFSET, ownership.manager
        )
        struct.pack_into(
            "<Q", payload, TARGET_EXPECTED_ALLOCATOR_OFFSET, ownership.allocator
        )
        struct.pack_into(
            "<I", payload, TARGET_EXPECTED_TARGET_OFFSET, ownership.target
        )
        struct.pack_into(
            "<I", payload, TARGET_EXPECTED_PALETTE_OFFSET, ownership.palette
        )
        if operation == TARGET_OPERATION_RETARGET:
            target = int(target) & 0xFFFFFFFF
            palette = int(palette)
            if not target or not 0 <= palette <= 6:
                raise ValueError("invalid target-circle UPDATE payload")
            variant = 1 if (target & 0xF00000) == 0x100000 else 2
            struct.pack_into("<I", payload, TARGET_HANDLE_OFFSET, target)
            struct.pack_into("<I", payload, TARGET_VARIANT_OFFSET, variant)
            struct.pack_into(
                "<I", payload, TARGET_DESIRED_PALETTE_OFFSET, palette
            )
        return bytes(payload)

    def _execute_owned(
        self,
        ownership: TargetCircleOwnership,
        *,
        operation: int,
        target: int = 0,
        palette: int = 0,
    ) -> TargetCircleOwnership | None:
        payload = self._owned_payload(
            ownership,
            operation=operation,
            target=target,
            palette=palette,
        )
        try:
            sequence = self.arm(payload)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                dispatch_state = self.process.u32(self.state)
                if dispatch_state == 3:
                    if (
                        self.process.u32(
                            self.state + TARGET_ACK_SEQUENCE_OFFSET
                        )
                        != sequence
                    ):
                        raise RuntimeError(
                            "target-circle dispatcher returned a stale result"
                        )
                    observed_target = self.process.u32(
                        self.state + TARGET_OBSERVED_OFFSET
                    )
                    observed_palette = self.process.u32(
                        self.state + TARGET_OBSERVED_PALETTE_OFFSET
                    )
                    if not observed_target or not 0 <= observed_palette <= 6:
                        raise RuntimeError(
                            "target-circle owned transaction returned invalid fields"
                        )
                    publication = self.ownership(ownership.owner_tag)
                    if operation == TARGET_OPERATION_RETIRE:
                        if publication is not None:
                            raise RuntimeError(
                                "retired target-circle ownership is still resident"
                            )
                        return None
                    if publication is None or not (
                        publication.owner_tag == ownership.owner_tag
                        and publication.token == ownership.token
                        and publication.manager == ownership.manager
                        and publication.allocator == ownership.allocator
                        and publication.node == ownership.node
                        and publication.target == observed_target
                        and publication.palette == observed_palette
                    ):
                        raise RuntimeError(
                            "target-circle owned transaction publication mismatch"
                        )
                    return publication
                if dispatch_state == 4:
                    raise RuntimeError(
                        "target-circle dispatcher rejected authenticated context"
                    )
                time.sleep(0.001)
            raise RuntimeError(
                "target-circle owned transaction timed out "
                f"(state={self.process.u32(self.state)})"
            )
        finally:
            self.maintain()

    def retarget(
        self,
        ownership: TargetCircleOwnership,
        target: int,
        *,
        palette: int | None = None,
    ) -> int:
        """Compatibility facade; unauthenticated node addresses are rejected."""

        if not isinstance(ownership, TargetCircleOwnership):
            raise TypeError("target-circle RETARGET requires explicit ownership")
        updated = self.update_ownership(
            ownership,
            target=target,
            palette=ownership.palette if palette is None else int(palette),
        )
        return updated.target

    def restore(self) -> bool:
        with _SERVICE_LOCK:
            if not self.maintain():
                return False
            state_image = self.process.read(self.state, STATE_SIZE)
            if _unpack_create_journal(state_image) is not None or any(
                _unpack_ownership_record(state_image, slot) is not None
                for slot in range(TARGET_OWNERSHIP_RECORD_COUNT)
            ):
                return False
            try:
                self.process.write(self.state, b"\x00" * 4)
            except OSError:
                return False
            self.patched = False
            self.quarantined = False
            self.owns_static = False
            return True


__all__ = [
    "CODE_OFFSET",
    "NativeTargetCircleRetargeter",
    "ORIGINAL_TICK_CALL",
    "P1_TARGET_OWNER_TAG",
    "P2_TARGET_OWNER_TAG",
    "P3_TARGET_OWNER_TAG",
    "REGION_MAGIC",
    "REGION_SIZE",
    "SERVICE_OFFSET",
    "STATE_OFFSET",
    "STATE_SIZE",
    "TARGET_CIRCLE_RETARGET",
    "TARGET_CIRCLE_ALLOCATOR_OFFSET",
    "TARGET_ACK_SEQUENCE_OFFSET",
    "TARGET_AUTH_TOKEN_OFFSET",
    "TARGET_DESIRED_PALETTE_OFFSET",
    "TARGET_EXPECTED_ALLOCATOR_OFFSET",
    "TARGET_EXPECTED_MANAGER_OFFSET",
    "TARGET_EXPECTED_PALETTE_OFFSET",
    "TARGET_EXPECTED_TARGET_OFFSET",
    "TARGET_FACTORY_ENTERED_OFFSET",
    "TARGET_HANDLE_OFFSET",
    "TARGET_NODE_OFFSET",
    "TARGET_OBSERVED_OFFSET",
    "TARGET_OBSERVED_PALETTE_OFFSET",
    "TARGET_OPERATION_CREATE",
    "TARGET_OPERATION_OFFSET",
    "TARGET_OPERATION_RECONCILE",
    "TARGET_OPERATION_RETIRE",
    "TARGET_OPERATION_RETARGET",
    "TARGET_OWNER_TAGS",
    "TARGET_OWNER_OFFSET",
    "TARGET_OWNERSHIP_MAGIC",
    "TARGET_OWNERSHIP_MAGIC_OFFSET",
    "TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET",
    "TARGET_OWNERSHIP_RECORDS_END",
    "TARGET_OWNERSHIP_RECORDS_OFFSET",
    "TARGET_OWNERSHIP_RECORD_COUNT",
    "TARGET_OWNERSHIP_RECORD_SIZE",
    "TARGET_OWNERSHIP_VERSION",
    "TARGET_RESULT_ALLOCATOR_OFFSET",
    "TARGET_RESULT_MANAGER_OFFSET",
    "TARGET_RESULT_RESERVED_OFFSET",
    "TARGET_REQUEST_RESERVED_OFFSET",
    "TARGET_SEQUENCE_OFFSET",
    "TARGET_STATE_MAGIC",
    "TARGET_STATE_MAGIC_OFFSET",
    "TARGET_UI_MANAGER_GLOBAL",
    "TARGET_VARIANT_OFFSET",
    "TargetCircleCreateJournal",
    "TargetCircleOwnership",
    "UI_MANAGER_TICK",
    "UI_MANAGER_TICK_CALL",
    "UI_MANAGER_TICK_RETURN",
    "_patch_rel32",
    "_rel32",
    "allocate_region",
    "build_retarget_dispatcher",
    "build_target_dispatcher",
]
