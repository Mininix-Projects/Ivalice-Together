import struct
import sys
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

import coop_runtime as coop  # noqa: E402
import movement_mailbox  # noqa: E402
import session_state  # noqa: E402


class _SparseMemoryProcess:
    def __init__(self):
        self.memory = {}
        self.writes = []

    def read(self, address, size):
        return bytes(self.memory.get(int(address) + index, 0) for index in range(size))

    def write(self, address, data):
        payload = bytes(data)
        for index, value in enumerate(payload):
            self.memory[int(address) + index] = value
        self.writes.append((int(address), payload))

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]


def _command(actor, phys, member, generation, world):
    return movement_mailbox.MovementCommand(
        True,
        actor,
        phys,
        member,
        generation,
        world,
        0.25 + member,
        0.5 + member,
        0.75,
    )


def _commit_reservation(process, address, reservation):
    process.write(address, struct.pack("<I", reservation.sequence | 1))
    process.write(address + 4, reservation.payload)
    process.write(address, struct.pack("<I", reservation.sequence))


def _applied_transfer(p2_sequence, p3_sequence):
    return coop.NativeLeaderRoleTransfer(
        sequence=17,
        target_member_id=1,
        identity=session_state.SessionIdentity(1, 5, 0),
        prepared_sequence=17,
        p2_movement_sequence=p2_sequence,
        p3_movement_sequence=p3_sequence,
        p1_hud_slot=1,
        p2_hud_slot=2,
        p3_hud_slot=0,
        committed_sequence=17,
        world_epoch=9,
        layout_generation=0x1234,
        applied_sequence=17,
    )


def test_applied_role_transfer_repairs_legacy_actor_mirrors_once():
    coop.set_cave_base(0x50000000)
    coop.set_dual_interaction_base(0x51000000)
    process = _SparseMemoryProcess()
    p2_publisher = movement_mailbox.publisher_for(process, coop.P2_MOVEMENT_MAILBOX)
    p3_publisher = movement_mailbox.publisher_for(process, coop.P3_MOVEMENT_MAILBOX)

    assert p2_publisher.publish(_command(0x100000, 0x101000, 1, 1, 9))
    assert p3_publisher.publish(_command(0x200000, 0x201000, 2, 1, 9))
    p2_desired = _command(0x300000, 0x301000, 5, 2, 9)
    p3_desired = _command(0x400000, 0x401000, 0, 2, 9)
    p2_reservation = p2_publisher.reserve(p2_desired)
    p3_reservation = p3_publisher.reserve(p3_desired)
    _commit_reservation(process, coop.P2_MOVEMENT_MAILBOX, p2_reservation)
    _commit_reservation(process, coop.P3_MOVEMENT_MAILBOX, p3_reservation)

    # Reproduce the transient state seen in the game: both mailbox commits are
    # correct, while compatibility consumers still see the new P1 actor as P2.
    process.write(coop.COMMAND_BLOCK + 0x10, struct.pack("<Q", 0xDEAD0000))
    process.write(coop.COMMAND_BLOCK + 0x78, struct.pack("<Q", 0xDEAD1000))
    p3_block = coop.third_player_block()
    process.write(p3_block + coop.P3_MOVEMENT_ACTIVE_OFFSET, struct.pack("<I", 1))
    process.write(p3_block + coop.P3_ACTOR_OFFSET, struct.pack("<Q", 0xBEEF0000))
    process.write(p3_block + coop.P3_PHYS_OFFSET, struct.pack("<Q", 0xBEEF1000))

    transfer = _applied_transfer(p2_reservation.sequence, p3_reservation.sequence)
    assert coop.settle_native_leader_role_transfer_publishers(process, transfer)
    assert process.u64(coop.COMMAND_BLOCK + 0x10) == p2_desired.actor
    assert process.u64(coop.COMMAND_BLOCK + 0x78) == p2_desired.phys
    assert process.u64(p3_block + coop.P3_ACTOR_OFFSET) == p3_desired.actor
    assert process.u64(p3_block + coop.P3_PHYS_OFFSET) == p3_desired.phys
    assert process.u32(p3_block + coop.P3_MOVEMENT_ACTIVE_OFFSET) == 1

    writes_after_first_settlement = len(process.writes)
    assert coop.settle_native_leader_role_transfer_publishers(process, transfer)
    assert len(process.writes) == writes_after_first_settlement


def test_uncommitted_reservation_does_not_touch_legacy_actor_mirror():
    coop.set_cave_base(0x52000000)
    coop.set_dual_interaction_base(0x53000000)
    process = _SparseMemoryProcess()
    p2_publisher = movement_mailbox.publisher_for(process, coop.P2_MOVEMENT_MAILBOX)
    p3_publisher = movement_mailbox.publisher_for(process, coop.P3_MOVEMENT_MAILBOX)
    assert p2_publisher.publish(_command(0x100000, 0x101000, 1, 1, 9))
    assert p3_publisher.publish(_command(0x200000, 0x201000, 2, 1, 9))
    p2_reservation = p2_publisher.reserve(_command(0x300000, 0x301000, 5, 2, 9))
    p3_reservation = p3_publisher.reserve(_command(0x400000, 0x401000, 0, 2, 9))
    stale_actor = 0xDEAD0000
    process.write(coop.COMMAND_BLOCK + 0x10, struct.pack("<Q", stale_actor))

    transfer = _applied_transfer(p2_reservation.sequence, p3_reservation.sequence)
    assert not coop.settle_native_leader_role_transfer_publishers(process, transfer)
    assert process.u64(coop.COMMAND_BLOCK + 0x10) == stale_actor
    assert not hasattr(process, "_native_leader_legacy_mirror_generations")
