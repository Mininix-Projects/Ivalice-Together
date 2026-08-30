import inspect
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

import coop_runtime as coop  # noqa: E402
import movement_mailbox  # noqa: E402
import session_state  # noqa: E402


MAIN_BASE = 0x50000000
DUAL_BASE = 0x51000000


class SparseProcess:
    def __init__(self):
        self.memory = {}
        self.writes = []
        self.landing_fault_addresses = set()
        self.closed = False

    def read(self, address, size):
        return bytes(self.memory.get(int(address) + index, 0) for index in range(size))

    def write(self, address, payload):
        address = int(address)
        payload = bytes(payload)
        for index, value in enumerate(payload):
            self.memory[address + index] = value
        self.writes.append((address, payload))
        if address in self.landing_fault_addresses:
            self.landing_fault_addresses.remove(address)
            raise OSError("remote write landed before failure was reported")

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def close(self):
        self.closed = True


class ActorGenerations:
    def observe(self, *_args, **_kwargs):
        return SimpleNamespace(actor_generation=11, native_handle=1)


def _command(actor, member, world):
    return movement_mailbox.MovementCommand(
        True,
        actor,
        actor + 0x800,
        member,
        3,
        world,
        0.25,
        0.5,
        0.75,
    )


def _seed_abandoned_role_claim(
    process,
    *,
    request=0,
    applied=0,
    claimed=0,
    committed=0,
):
    base = coop.P2_DEVICE_FILTER_STATE
    for offset, value in (
        (coop.NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET, request),
        (coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET, applied),
        (coop.NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET, claimed),
        (coop.NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET, committed),
        (coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET, coop.NATIVE_LEADER_ROLE_CLAIMING),
    ):
        process.write(base + offset, struct.pack("<I", value))
    process.write(
        coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
        struct.pack("<I", 1),
    )


def _prepare_faulted_transaction(*, fault_at="prepared"):
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    battle_info = 0x62000000
    location_base = 0x63000000
    actor_manager = 0x64000000
    location_id = 0x1234
    process.write(
        session_state.BATTLE_INFO_GLOBAL,
        struct.pack("<Q", battle_info),
    )
    process.write(
        session_state.LOCATION_SAVE_GLOBAL,
        struct.pack("<Q", location_base),
    )
    process.write(coop.ACTOR_MANAGER_GLOBAL, struct.pack("<Q", actor_manager))
    process.write(actor_manager, struct.pack("<I", 2))
    for layout_index in (
        session_state.BATTLE_CURRENT_LAYOUT,
        session_state.BATTLE_UI_LAYOUT,
    ):
        process.write(
            battle_info
            + session_state.BATTLE_PARTY_LAYOUT_OFFSET
            + session_state.BATTLE_PARTY_LAYOUT_STRIDE * layout_index,
            struct.pack("<4H", *layout),
        )
    process.write(
        location_base + session_state.LOCATION_CURRENT_OFFSET,
        struct.pack("<I", location_id),
    )
    process.write(
        location_base + session_state.LOCATION_UI_OFFSET,
        struct.pack("<I", location_id),
    )
    old_identity = session_state.SessionIdentity(0, 1, 2)
    sequence, desired_identity = coop.request_native_leader_role_transfer(
        process,
        1,
        old_identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    snapshot = SimpleNamespace(
        identity=old_identity,
        current_layout=layout,
        ui_layout=layout,
        loading=False,
        cutscene=False,
        leader_party_member_id=0,
        battle_info=battle_info,
        current_location=location_id,
        ui_location=location_id,
    )
    p2_publisher = movement_mailbox.publisher_for(
        process, coop.P2_MOVEMENT_MAILBOX
    )
    p3_publisher = movement_mailbox.publisher_for(
        process, coop.P3_MOVEMENT_MAILBOX
    )
    assert p2_publisher.publish(_command(0x60000000, 1, 9))
    assert p3_publisher.publish(_command(0x60010000, 2, 9))
    old_p2 = process.read(coop.P2_MOVEMENT_MAILBOX, movement_mailbox.MAILBOX_SIZE)
    old_p3 = process.read(coop.P3_MOVEMENT_MAILBOX, movement_mailbox.MAILBOX_SIZE)
    transfer = coop.read_native_leader_role_transfer(process)
    assert transfer is not None and transfer.sequence == sequence
    fault_addresses = {
        "prepared": (
            coop.P2_DEVICE_FILTER_STATE
            + coop.NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET
        ),
        "journal-odd": coop.NATIVE_LEADER_ROLE_JOURNAL,
        "journal-body": coop.NATIVE_LEADER_ROLE_JOURNAL + 4,
    }
    if fault_at is not None:
        process.landing_fault_addresses.add(fault_addresses[fault_at])

    with (
        mock.patch.object(
            coop.session_state,
            "map_party_slot_actors",
            return_value={
                0: (0x60020000, 0x60020800),
                1: (0x60030000, 0x60030800),
            },
        ),
        mock.patch.object(coop, "fresh_role_actor_mapping", side_effect=lambda entry: entry),
        mock.patch.object(coop, "actor_is_alive", return_value=True),
        mock.patch.object(coop, "actor_u32_or_zero", return_value=0xA11CE),
        mock.patch.object(coop, "leader_heading_pair", return_value=(0.0, 0.0)),
    ):
        reported = coop.prepare_native_leader_role_transfer(
            process,
            transfer,
            snapshot,
            {},
            ActorGenerations(),
            world_epoch=9,
            manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
        )

    return (
        process,
        sequence,
        desired_identity,
        reported,
        old_p2,
        old_p3,
    )


def test_p1_swap_with_p2_preserves_dead_p3_role_identity():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    layout = (0, 4, 1, session_state.EMPTY_PARTY_ID)
    current_identity = session_state.SessionIdentity(1, 0, 4)

    sequence, replacement = coop.request_native_leader_role_transfer(
        process,
        0,
        current_identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )

    assert sequence == 1
    assert replacement == session_state.SessionIdentity(0, 1, 4)
    transfer = coop.read_native_leader_role_transfer(process)
    assert transfer is not None
    assert transfer.identity == session_state.SessionIdentity(0, 1, 4)
    assert transfer.identity.p3_party_member_id == current_identity.p3_party_member_id


def test_prepared_write_lands_then_raises_preserves_reservation_and_ends_frame():
    process, sequence, _identity, reported, old_p2, old_p3 = (
        _prepare_faulted_transaction()
    )
    transfer = coop.read_native_leader_role_transfer(process)
    p2_publisher = movement_mailbox.publisher_for(
        process, coop.P2_MOVEMENT_MAILBOX
    )

    assert reported
    assert transfer is not None and transfer.prepared
    assert p2_publisher.reserved is not None
    assert process.read(
        coop.P2_MOVEMENT_MAILBOX, movement_mailbox.MAILBOX_SIZE
    ) == old_p2
    assert process.read(
        coop.P3_MOVEMENT_MAILBOX, movement_mailbox.MAILBOX_SIZE
    ) == old_p3
    assert coop.native_leader_role_transfer_write_authority_crossed(
        transfer,
        prepare_reported=reported,
        claimed_sequence=0,
    )
    assert transfer.sequence == sequence


def test_restart_reconstructs_faulted_prepared_reservation_before_any_reset():
    process, sequence, _identity, _reported, old_p2, old_p3 = (
        _prepare_faulted_transaction()
    )
    del process._movement_mailbox_publishers
    writes_before = len(process.writes)

    assert coop.recover_native_leader_role_transfer_for_restart(process)

    p2_publisher = movement_mailbox.publisher_for(
        process, coop.P2_MOVEMENT_MAILBOX
    )
    assert p2_publisher.reserved is not None
    assert p2_publisher.reserved.sequence == 4
    assert process.read(
        coop.P2_MOVEMENT_MAILBOX, movement_mailbox.MAILBOX_SIZE
    ) == old_p2
    assert process.read(
        coop.P3_MOVEMENT_MAILBOX, movement_mailbox.MAILBOX_SIZE
    ) == old_p3
    assert process.u32(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    ) == coop.NATIVE_LEADER_ROLE_ACTIVE
    assert len(process.writes) >= writes_before
    assert process.u32(
        coop.P2_DEVICE_FILTER_STATE
        + coop.NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET
    ) == sequence


def test_shutdown_preserves_every_remote_byte_during_role_transaction():
    process, _sequence, _identity, _reported, _old_p2, _old_p3 = (
        _prepare_faulted_transaction()
    )
    before = dict(process.memory)
    writes_before = list(process.writes)
    ko = SimpleNamespace(shutdown=mock.Mock())
    area = SimpleNamespace(shutdown=mock.Mock())

    coop.shutdown_runtime(
        process,
        ko_leader_ownership=ko,
        area_handoff=area,
        camera_tuning=SimpleNamespace(),
    )

    expected = dict(before)
    for index, value in enumerate(struct.pack("<I", 1)):
        expected[coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE + index] = value
    assert process.memory == expected
    assert process.writes == writes_before + [
        (coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE, struct.pack("<I", 1))
    ]
    assert process.closed
    ko.shutdown.assert_not_called()
    area.shutdown.assert_not_called()


def test_journal_seqlock_publishes_odd_scalar_before_any_payload_byte():
    process, _sequence, _identity, reported, _old_p2, _old_p3 = (
        _prepare_faulted_transaction(fault_at=None)
    )
    journal = coop.NATIVE_LEADER_ROLE_JOURNAL
    touches = [
        (address, payload)
        for address, payload in process.writes
        if journal <= address < journal + coop.NATIVE_LEADER_ROLE_JOURNAL_SIZE
    ]

    assert reported
    assert [address for address, _payload in touches[:3]] == [
        journal,
        journal + 4,
        journal,
    ]
    odd = struct.unpack("<I", touches[0][1])[0]
    even = struct.unpack("<I", touches[2][1])[0]
    assert len(touches[0][1]) == 4
    assert odd == even | 1
    assert len(touches[1][1]) == coop.NATIVE_LEADER_JOURNAL_RECORD_SIZE - 4


@pytest.mark.parametrize("fault_at", ("journal-odd", "journal-body"))
def test_journal_fault_keeps_odd_commit_and_never_publishes_prepared(fault_at):
    process, sequence, _identity, reported, old_p2, old_p3 = (
        _prepare_faulted_transaction(fault_at=fault_at)
    )
    transfer = coop.read_native_leader_role_transfer(process)
    p2_publisher = movement_mailbox.publisher_for(
        process, coop.P2_MOVEMENT_MAILBOX
    )

    assert not reported
    assert transfer is not None and not transfer.prepared
    assert process.u32(coop.NATIVE_LEADER_ROLE_JOURNAL) & 1
    assert p2_publisher.reserved is None
    assert process.read(
        coop.P2_MOVEMENT_MAILBOX, movement_mailbox.MAILBOX_SIZE
    ) == old_p2
    assert process.read(
        coop.P3_MOVEMENT_MAILBOX, movement_mailbox.MAILBOX_SIZE
    ) == old_p3
    assert process.u32(
        coop.P2_DEVICE_FILTER_STATE
        + coop.NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET
    ) != sequence


def test_unknown_role_mode_is_never_promoted_during_restart_recovery():
    process, _sequence, _identity, reported, _old_p2, _old_p3 = (
        _prepare_faulted_transaction(fault_at=None)
    )
    assert reported
    process.write(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        struct.pack("<I", 5),
    )
    process.writes.clear()

    assert coop.read_native_leader_role_transfer(
        process, include_inactive=True
    ) is None
    with pytest.raises(RuntimeError, match="cannot safely recover"):
        coop.recover_native_leader_role_transfer_for_restart(process)

    assert process.writes == []


def test_stable_layout_change_cancels_only_after_three_coherent_samples():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    _sequence, _replacement = coop.request_native_leader_role_transfer(
        process,
        1,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    transfer = coop.read_native_leader_role_transfer(process)
    changed_layout = (0, 1, 3, session_state.EMPTY_PARTY_ID)
    snapshot = SimpleNamespace(
        identity=identity,
        battle_info=0x62000000,
        current_layout=changed_layout,
        ui_layout=changed_layout,
        leader_party_member_id=0,
        current_location=10,
        ui_location=10,
        user_control_enabled=True,
        native_menu_open=False,
        native_modal_active=False,
        loading=False,
        cutscene=False,
    )
    tracker = coop.NativeLeaderRolePrepareCancellationTracker()

    assert not tracker.observe(transfer, snapshot)
    assert not tracker.observe(transfer, snapshot)
    assert tracker.observe(transfer, snapshot)
    assert coop.cancel_native_leader_role_transfer_before_prepared(
        process,
        transfer,
    )

    base = coop.P2_DEVICE_FILTER_STATE
    assert (
        process.u32(base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET)
        == coop.NATIVE_LEADER_ROLE_CANCELLED
    )
    assert process.u32(
        base + coop.NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET
    ) == process.u32(base + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET)
    assert coop.read_native_leader_role_transfer(process) is None
    assert coop.native_leader_role_transfer_cancelled(
        process,
        transfer.sequence,
    )
    assert coop.native_leader_role_cancellation_pending(process)
    assert coop.acknowledge_native_leader_role_cancellation(
        process,
        transfer.sequence,
    ) == transfer.sequence
    assert process.u32(base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET) == 0
    next_sequence, _next_identity = coop.request_native_leader_role_transfer(
        process,
        2,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    assert next_sequence != transfer.sequence


def test_terminal_cancel_blocks_both_producers_until_idempotent_ack():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    sequence, _replacement = coop.request_native_leader_role_transfer(
        process,
        1,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    process.write(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        struct.pack("<I", coop.NATIVE_LEADER_ROLE_CANCELLING),
    )
    assert coop._recover_native_leader_role_cancellation(process)  # noqa: SLF001
    terminal = dict(process.memory)

    assert not coop.claim_native_leader_transient_handoff(process)
    with pytest.raises(RuntimeError, match="already published|cancellation"):
        coop.request_native_leader_role_transfer(
            process,
            2,
            identity,
            layout,
            layout,
            manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
        )
    assert process.memory == terminal

    assert coop.acknowledge_native_leader_role_cancellation(process, sequence) == 0
    assert coop.settle_native_leader_role_cancellation(process) == sequence
    assert coop.acknowledge_native_leader_role_cancellation(
        process, sequence
    ) == sequence
    assert coop.acknowledge_native_leader_role_cancellation(
        process, sequence
    ) == sequence
    assert coop.claim_native_leader_transient_handoff(process)
    coop.publish_native_leader_handoff_active(process, False)
    next_sequence, _next_identity = coop.request_native_leader_role_transfer(
        process,
        2,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    assert next_sequence == (sequence + 1) & 0xFFFFFFFF


@pytest.mark.parametrize(
    "crash_after",
    (
        "cancelling",
        "prepared-withdrawal",
        "tombstone",
        "applied",
    ),
)
def test_prepared_world_stale_cancellation_recovers_every_native_store(
    crash_after,
):
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    sequence, _replacement = coop.request_native_leader_role_transfer(
        process,
        1,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    base = coop.P2_DEVICE_FILTER_STATE
    process.write(
        base + coop.NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET,
        struct.pack("<I", sequence),
    )
    process.write(
        base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        struct.pack("<I", coop.NATIVE_LEADER_ROLE_CANCELLING),
    )
    if crash_after != "cancelling":
        process.write(
            base + coop.NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET,
            struct.pack("<I", 0),
        )
    if crash_after in ("tombstone", "applied"):
        process.write(
            coop.NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE,
            struct.pack("<I", sequence),
        )
    if crash_after == "applied":
        process.write(
            base + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
            struct.pack("<I", sequence),
        )

    assert coop._recover_native_leader_role_cancellation(process)  # noqa: SLF001
    assert process.u32(
        base + coop.NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET
    ) == 0
    assert process.u32(coop.NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE) == sequence
    assert process.u32(base + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET) == sequence
    assert (
        process.u32(base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET)
        == coop.NATIVE_LEADER_ROLE_CANCELLED
    )
    assert coop.settle_native_leader_role_cancellation(process) == sequence
    assert coop.acknowledge_native_leader_role_cancellation(
        process, sequence
    ) == sequence
    assert not coop.native_leader_role_transfer_recovery_required(process)

    next_sequence, _next_identity = coop.request_native_leader_role_transfer(
        process,
        2,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    assert next_sequence == (sequence + 1) & 0xFFFFFFFF


def test_cancelling_applied_without_tombstone_never_withdraws_prepared():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    sequence, _replacement = coop.request_native_leader_role_transfer(
        process,
        1,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    base = coop.P2_DEVICE_FILTER_STATE
    process.write(
        base + coop.NATIVE_LEADER_ROLE_PREPARED_SEQUENCE_OFFSET,
        struct.pack("<I", sequence),
    )
    process.write(
        base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        struct.pack("<I", coop.NATIVE_LEADER_ROLE_CANCELLING),
    )
    process.write(
        base + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
        struct.pack("<I", sequence),
    )
    before = dict(process.memory)

    with pytest.raises(RuntimeError, match="crossed commit authority"):
        coop._recover_native_leader_role_cancellation(process)  # noqa: SLF001

    assert process.memory == before


@pytest.mark.parametrize(
    "fault_address",
    (
        "ack",
        "release",
    ),
)
def test_terminal_cancel_ack_accepts_land_then_raise_and_retries_idempotently(
    fault_address,
):
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    sequence, _replacement = coop.request_native_leader_role_transfer(
        process,
        1,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    process.write(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        struct.pack("<I", coop.NATIVE_LEADER_ROLE_CANCELLING),
    )
    assert coop._recover_native_leader_role_cancellation(process)  # noqa: SLF001
    assert coop.settle_native_leader_role_cancellation(process) == sequence
    address = (
        coop.NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE
        if fault_address == "ack"
        else coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    )
    process.landing_fault_addresses.add(address)

    assert coop.acknowledge_native_leader_role_cancellation(
        process, sequence
    ) == sequence
    assert coop.acknowledge_native_leader_role_cancellation(
        process, sequence
    ) == sequence
    assert process.u32(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    ) == 0


def test_transient_layout_change_never_reaches_cancel_threshold():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    coop.request_native_leader_role_transfer(
        process,
        1,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    transfer = coop.read_native_leader_role_transfer(process)
    changed_layout = (0, 1, 3, session_state.EMPTY_PARTY_ID)
    changed = SimpleNamespace(
        identity=identity,
        battle_info=0x62000000,
        current_layout=changed_layout,
        ui_layout=changed_layout,
        leader_party_member_id=0,
        current_location=10,
        ui_location=10,
        user_control_enabled=True,
        native_menu_open=False,
        native_modal_active=False,
        loading=False,
        cutscene=False,
    )
    coherent = SimpleNamespace(
        identity=identity,
        battle_info=0x62000000,
        current_layout=layout,
        ui_layout=layout,
        leader_party_member_id=0,
        current_location=10,
        ui_location=10,
        user_control_enabled=True,
        native_menu_open=False,
        native_modal_active=False,
        loading=False,
        cutscene=False,
    )
    loading = SimpleNamespace(
        identity=identity,
        battle_info=0x62000000,
        current_layout=changed_layout,
        ui_layout=changed_layout,
        leader_party_member_id=0,
        current_location=10,
        ui_location=10,
        user_control_enabled=False,
        native_menu_open=False,
        native_modal_active=False,
        loading=True,
        cutscene=False,
    )
    tracker = coop.NativeLeaderRolePrepareCancellationTracker()

    assert not tracker.observe(transfer, changed)
    assert not tracker.observe(transfer, changed)
    assert not tracker.observe(transfer, loading)
    assert not tracker.observe(transfer, changed)
    assert not tracker.observe(transfer, coherent)
    assert not tracker.observe(transfer, changed)
    assert process.u32(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    ) == coop.NATIVE_LEADER_ROLE_ACTIVE


def test_removed_old_p1_is_quarantined_despite_identity_relative_loading():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    coop.request_native_leader_role_transfer(
        process,
        1,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    transfer = coop.read_native_leader_role_transfer(process)
    converged_without_old_p1 = (1, 2, 3, session_state.EMPTY_PARTY_ID)
    snapshot = SimpleNamespace(
        identity=identity,
        battle_info=0x62000000,
        current_layout=converged_without_old_p1,
        ui_layout=converged_without_old_p1,
        leader_party_member_id=1,
        current_location=10,
        ui_location=10,
        user_control_enabled=True,
        native_menu_open=False,
        native_modal_active=False,
        loading=True,
        cutscene=False,
    )
    tracker = coop.NativeLeaderRolePrepareCancellationTracker()

    assert not tracker.observe(transfer, snapshot)
    assert tracker.samples == 1
    assert not tracker.observe(transfer, snapshot)
    assert tracker.samples == 2
    assert tracker.observe(transfer, snapshot)
    assert coop.cancel_native_leader_role_transfer_before_prepared(
        process,
        transfer,
    )
    assert (
        process.u32(
            coop.P2_DEVICE_FILTER_STATE
            + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
        )
        == coop.NATIVE_LEADER_ROLE_CANCELLED
    )


def test_dead_requested_p1_is_a_stable_prepared_cancellation_reason():
    identity = session_state.SessionIdentity(0, 1, 2)
    desired = session_state.SessionIdentity(1, 0, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    transfer = SimpleNamespace(
        sequence=7,
        prepared=False,
        native_applied=False,
        identity=desired,
        layout_generation=coop.leader_role_layout_generation(layout, layout),
    )
    snapshot = SimpleNamespace(
        identity=identity,
        battle_info=0x62000000,
        current_layout=layout,
        ui_layout=layout,
        loading=False,
        cutscene=False,
        user_control_enabled=True,
        native_menu_open=False,
        native_modal_active=False,
    )
    tracker = coop.NativeLeaderRolePrepareCancellationTracker()
    with (
        mock.patch.object(
            coop.session_state,
            "map_party_slot_actors",
            return_value={1: (0x60000000, 0x60000800)},
        ),
        mock.patch.object(
            coop,
            "fresh_role_actor_mapping",
            side_effect=lambda entry: entry,
        ),
        mock.patch.object(coop, "actor_is_alive", return_value=False),
        mock.patch.object(coop, "actor_u32_or_zero", return_value=0x100001),
    ):
        signature = coop.native_leader_role_target_unavailable_signature(
            mock.sentinel.process,
            transfer,
            snapshot,
            {},
        )

    assert signature[1] == "target-dead"
    assert not tracker.observe_signature(signature)
    assert not tracker.observe_signature(signature)
    assert tracker.observe_signature(signature)


def test_stone_requested_p1_is_a_stable_prepared_cancellation_reason():
    identity = session_state.SessionIdentity(0, 1, 2)
    desired = session_state.SessionIdentity(1, 0, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    transfer = SimpleNamespace(
        sequence=8,
        prepared=False,
        native_applied=False,
        identity=desired,
        layout_generation=coop.leader_role_layout_generation(layout, layout),
    )
    snapshot = SimpleNamespace(
        identity=identity,
        battle_info=0x62000000,
        current_layout=layout,
        ui_layout=layout,
        loading=False,
        cutscene=False,
        user_control_enabled=True,
        native_menu_open=False,
        native_modal_active=False,
    )
    tracker = coop.NativeLeaderRolePrepareCancellationTracker()
    with (
        mock.patch.object(
            coop.session_state,
            "map_party_slot_actors",
            return_value={1: (0x60000000, 0x60000800)},
        ),
        mock.patch.object(
            coop,
            "fresh_role_actor_mapping",
            side_effect=lambda entry: entry,
        ),
        mock.patch.object(coop, "actor_is_alive", return_value=True),
        mock.patch.object(
            coop,
            "actor_available_for_native_leader",
            return_value=False,
        ),
        mock.patch.object(coop, "actor_u32_or_zero", return_value=0x100001),
    ):
        signature = coop.native_leader_role_target_unavailable_signature(
            mock.sentinel.process,
            transfer,
            snapshot,
            {},
        )

    assert signature[1] == "target-command-blocked"
    assert not tracker.observe_signature(signature)
    assert not tracker.observe_signature(signature)
    assert tracker.observe_signature(signature)


def test_prepared_mismatch_quarantine_precedes_every_ordinary_frame_writer():
    source = inspect.getsource(coop.main)
    loop = source.index("while True:")
    structural_sample = source.index(
        "structural_snapshot = session.snapshot(",
        loop,
    )
    quarantine = source.index(
        "leader_role_cancel_tracker.observe(",
        structural_sample,
    )
    area_writer = source.index("area_handoff.tick(process, now)", loop)
    input_writer = source.index("bindings.begin_frame(", loop)
    resolver = source.index("resolve_active_actors(process)", loop)

    assert loop < structural_sample < quarantine
    assert quarantine < area_writer < input_writer < resolver


def test_restart_finishes_movement_owned_prepared_cancel_idempotently():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    sequence, _replacement = coop.request_native_leader_role_transfer(
        process,
        1,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    base = coop.P2_DEVICE_FILTER_STATE
    process.write(
        base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        struct.pack("<I", coop.NATIVE_LEADER_ROLE_CANCELLING),
    )

    assert coop.recover_native_leader_role_transfer_for_restart(process)
    assert (
        process.u32(base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET)
        == coop.NATIVE_LEADER_ROLE_CANCELLED
    )
    assert process.u32(
        base + coop.NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET
    ) == process.u32(base + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET)
    assert coop.native_leader_role_transfer_cancelled(process, sequence)
    assert coop.native_leader_role_cancellation_pending(process)

    terminal = dict(process.memory)
    coop._reset_runtime_dynamic_state(  # noqa: SLF001
        process,
        coop.DEFAULT_MANUAL_CONTROL_MASK,
        role_producer_quiesced=True,
    )
    assert process.memory == terminal

    assert coop.acknowledge_native_leader_role_cancellation(
        process,
        sequence,
    ) == sequence
    assert process.u32(base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET) == 0
    next_sequence, _next_identity = coop.request_native_leader_role_transfer(
        process,
        2,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    assert next_sequence != sequence


@pytest.mark.parametrize(
    "published_scalars",
    ((), ("marker",), ("marker", "applied")),
    ids=("after-claim", "after-marker", "after-applied"),
)
def test_restart_finishes_cancel_after_every_terminal_scalar(published_scalars):
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    sequence, _replacement = coop.request_native_leader_role_transfer(
        process,
        1,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    base = coop.P2_DEVICE_FILTER_STATE
    process.write(
        base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        struct.pack("<I", coop.NATIVE_LEADER_ROLE_CANCELLING),
    )
    if "marker" in published_scalars:
        process.write(
            coop.NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE,
            struct.pack("<I", sequence),
        )
    if "applied" in published_scalars:
        process.write(
            base + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
            struct.pack("<I", sequence),
        )

    assert coop.recover_native_leader_role_transfer_for_restart(process)
    assert process.u32(
        base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    ) == coop.NATIVE_LEADER_ROLE_CANCELLED
    assert coop.native_leader_role_cancellation_pending(process)


def test_restart_rejects_cancel_applied_without_authenticated_tombstone():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    sequence, _replacement = coop.request_native_leader_role_transfer(
        process,
        1,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    base = coop.P2_DEVICE_FILTER_STATE
    process.write(
        base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        struct.pack("<I", coop.NATIVE_LEADER_ROLE_CANCELLING),
    )
    process.write(
        base + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
        struct.pack("<I", sequence),
    )

    with pytest.raises(RuntimeError, match="cannot safely recover"):
        coop.recover_native_leader_role_transfer_for_restart(process)


def test_role_outcome_never_reports_applied_across_a_cancellation_race():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    sequence, _replacement = coop.request_native_leader_role_transfer(
        process,
        1,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    base = coop.P2_DEVICE_FILTER_STATE
    original_read = process.read
    original_write = process.write
    ack_reads = 0

    def racing_read(address, size):
        nonlocal ack_reads
        raw = original_read(address, size)
        if (
            int(address) == coop.NATIVE_LEADER_ROLE_CANCEL_ACK_SEQUENCE
            and int(size) == 4
        ):
            ack_reads += 1
            if ack_reads == 1:
                original_write(
                    coop.NATIVE_LEADER_ROLE_CANCELLED_SEQUENCE,
                    struct.pack("<I", sequence),
                )
                original_write(
                    base + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
                    struct.pack("<I", sequence),
                )
                original_write(
                    base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
                    struct.pack("<I", coop.NATIVE_LEADER_ROLE_CANCELLED),
                )
        return raw

    process.read = racing_read
    assert coop.native_leader_role_transfer_outcome(process, sequence) == "pending"
    assert coop.native_leader_role_transfer_outcome(process, sequence) == "cancelled"


def test_closed_shutdown_fence_rejects_role_request_without_claiming():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    process.write(
        coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
        struct.pack("<I", 1),
    )
    process.writes.clear()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)

    with pytest.raises(RuntimeError, match="fenced"):
        coop.request_native_leader_role_transfer(
            process,
            1,
            identity,
            layout,
            layout,
            manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
        )

    assert process.writes == []
    assert process.u32(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    ) == 0
    assert process.u32(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET
    ) == 0


def test_first_claim_land_raise_with_failed_local_clear_recovers_on_restart():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    role_active = (
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    )
    original_write = process.write
    first_claim = True

    def ambiguous_write(address, payload):
        nonlocal first_claim
        address = int(address)
        payload = bytes(payload)
        if address == role_active and payload == struct.pack(
            "<I", coop.NATIVE_LEADER_ROLE_CLAIMING
        ):
            assert first_claim
            first_claim = False
            original_write(address, payload)
            raise OSError("CLAIMING landed before failure was reported")
        if address == role_active and payload == struct.pack("<I", 0):
            raise OSError("local rollback did not land")
        original_write(address, payload)

    process.write = ambiguous_write
    with pytest.raises(OSError, match="CLAIMING landed"):
        coop.request_native_leader_role_transfer(
            process,
            1,
            identity,
            layout,
            layout,
            manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
        )

    assert process.u32(role_active) == coop.NATIVE_LEADER_ROLE_CLAIMING
    assert process.u32(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET
    ) == process.u32(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET
    )
    process.write = original_write
    process.write(
        coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
        struct.pack("<I", 1),
    )

    assert not coop.recover_native_leader_role_transfer_for_restart(
        process,
        producer_quiesced=True,
    )
    assert process.u32(role_active) == 0


@pytest.mark.parametrize("baseline", (0, 9))
def test_restart_clears_pre_request_claim_only_under_closed_fence(baseline):
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    _seed_abandoned_role_claim(
        process,
        request=baseline,
        applied=baseline,
        claimed=baseline,
        committed=baseline,
    )

    assert not coop.recover_native_leader_role_transfer_for_restart(
        process,
        producer_quiesced=True,
    )
    assert process.u32(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    ) == 0


def test_many_consecutive_transfers_preserve_the_previous_terminal_claim_pair():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    identity = session_state.SessionIdentity(0, 1, 2)
    previous_sequence = 0
    base = coop.P2_DEVICE_FILTER_STATE

    for _iteration in range(64):
        target = (identity.p1_party_member_id + 1) % 3
        sequence, identity = coop.request_native_leader_role_transfer(
            process,
            target,
            identity,
            layout,
            layout,
            manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
        )

        assert sequence != previous_sequence
        assert process.u32(
            base + coop.NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET
        ) == previous_sequence
        assert process.u32(
            base + coop.NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET
        ) == previous_sequence

        for offset in (
            coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
            coop.NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET,
            coop.NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET,
        ):
            process.write(base + offset, struct.pack("<I", sequence))
        process.write(
            base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
            struct.pack("<I", 0),
        )
        previous_sequence = sequence


def test_open_claim_freezes_without_promoting_previous_terminal_payload():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    sequence, _replacement = coop.request_native_leader_role_transfer(
        process,
        1,
        identity,
        layout,
        layout,
        manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
    )
    base = coop.P2_DEVICE_FILTER_STATE
    for offset in (
        coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
        coop.NATIVE_LEADER_ROLE_CLAIMED_SEQUENCE_OFFSET,
        coop.NATIVE_LEADER_ROLE_COMMITTED_SEQUENCE_OFFSET,
    ):
        process.write(base + offset, struct.pack("<I", sequence))
    process.write(
        base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET,
        struct.pack("<I", coop.NATIVE_LEADER_ROLE_CLAIMING),
    )
    process.writes.clear()

    # include_inactive is used by the ordinary headless snapshot path. It
    # must not reinterpret the preceding terminal payload as the new claim.
    assert coop.read_native_leader_role_transfer(
        process,
        include_inactive=True,
    ) is None
    assert coop.recover_native_leader_role_transfer_for_restart(process)

    assert process.u32(
        base + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    ) == coop.NATIVE_LEADER_ROLE_CLAIMING
    assert process.writes == []


@pytest.mark.parametrize(
    ("request_sequence", "applied", "claimed", "committed"),
    (
        (8, 7, 0, 0),
        (7, 7, 8, 7),
    ),
    ids=("request-advanced", "native-claim-uncommitted"),
)
def test_restart_never_clears_claim_after_any_irreversible_word_advanced(
    request_sequence,
    applied,
    claimed,
    committed,
):
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    _seed_abandoned_role_claim(
        process,
        request=request_sequence,
        applied=applied,
        claimed=claimed,
        committed=committed,
    )
    process.writes.clear()

    with pytest.raises(RuntimeError, match="cannot safely recover"):
        coop.recover_native_leader_role_transfer_for_restart(
            process,
            producer_quiesced=True,
        )

    assert process.u32(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    ) == coop.NATIVE_LEADER_ROLE_CLAIMING
    assert process.writes == []


def test_restart_accepts_ambiguous_claim_clear_only_after_exact_readback():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    _seed_abandoned_role_claim(process)
    role_active = (
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    )
    process.landing_fault_addresses.add(role_active)

    assert not coop.recover_native_leader_role_transfer_for_restart(
        process,
        producer_quiesced=True,
    )
    assert process.u32(role_active) == 0


def test_restart_preserves_claim_when_ambiguous_clear_cannot_be_proven():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    _seed_abandoned_role_claim(process)
    role_active = (
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    )
    original_write = process.write

    def failed_clear(address, payload):
        if int(address) == role_active and bytes(payload) == struct.pack("<I", 0):
            raise OSError("clear outcome is unknown")
        original_write(address, payload)

    process.write = failed_clear
    process.writes.clear()
    with pytest.raises(RuntimeError, match="cannot safely recover"):
        coop.recover_native_leader_role_transfer_for_restart(
            process,
            producer_quiesced=True,
        )

    assert process.u32(role_active) == coop.NATIVE_LEADER_ROLE_CLAIMING
    assert process.writes == []


def test_fence_closing_after_claim_aborts_before_request_publication():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    identity = session_state.SessionIdentity(0, 1, 2)
    layout = (0, 1, 2, session_state.EMPTY_PARTY_ID)
    original_write = process.write
    role_active = (
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    )

    def write(address, payload):
        original_write(address, payload)
        if int(address) == role_active and bytes(payload) == struct.pack(
            "<I", coop.NATIVE_LEADER_ROLE_CLAIMING
        ):
            original_write(
                coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
                struct.pack("<I", 1),
            )

    process.write = write
    with pytest.raises(RuntimeError, match="fence won"):
        coop.request_native_leader_role_transfer(
            process,
            1,
            identity,
            layout,
            layout,
            manual_control_mask=coop.DEFAULT_MANUAL_CONTROL_MASK,
        )

    assert process.u32(
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET
    ) == 0
    assert process.u32(role_active) == 0
    assert process.u32(coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE) == 1


def test_ambiguous_fence_write_authorizes_no_shutdown_cleanup():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    process.landing_fault_addresses.add(coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE)
    ko = SimpleNamespace(shutdown=mock.Mock())
    area = SimpleNamespace(shutdown=mock.Mock())

    coop.shutdown_runtime(
        process,
        ko_leader_ownership=ko,
        area_handoff=area,
        camera_tuning=SimpleNamespace(),
    )

    assert process.writes == [
        (coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE, struct.pack("<I", 1))
    ]
    assert process.closed
    ko.shutdown.assert_not_called()
    area.shutdown.assert_not_called()


def test_claim_visible_before_shutdown_fence_forces_remote_preservation():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    original_write = process.write
    role_active = (
        coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
    )

    def write(address, payload):
        if int(address) == coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE:
            original_write(
                role_active,
                struct.pack("<I", coop.NATIVE_LEADER_ROLE_CLAIMING),
            )
        original_write(address, payload)

    process.write = write
    ko = SimpleNamespace(shutdown=mock.Mock())
    area = SimpleNamespace(shutdown=mock.Mock())
    coop.shutdown_runtime(
        process,
        ko_leader_ownership=ko,
        area_handoff=area,
        camera_tuning=SimpleNamespace(),
    )

    assert [address for address, _payload in process.writes] == [
        role_active,
        coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
    ]
    assert process.closed
    ko.shutdown.assert_not_called()
    area.shutdown.assert_not_called()


def test_movement_reopens_admission_only_after_install_and_first_liveness():
    source = inspect.getsource(coop.main)
    install = source.index("install_runtime(")
    liveness = source.index("publish_movement_helper_liveness(", install)
    reopen = source.index(
        'label="movement ready role admission open"', liveness
    )
    loop = source.index("while True:", reopen)

    assert install < liveness < reopen < loop


def test_runtime_reset_preserves_closed_role_admission_fence():
    coop.set_cave_base(MAIN_BASE)
    coop.set_dual_interaction_base(DUAL_BASE)
    process = SparseProcess()
    process.write(
        coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE,
        struct.pack("<I", 1),
    )
    process.writes.clear()

    with mock.patch.object(
        coop,
        "recover_native_leader_role_transfer_for_restart",
        return_value=False,
    ):
        coop._reset_runtime_dynamic_state(  # noqa: SLF001
            process,
            coop.DEFAULT_MANUAL_CONTROL_MASK,
        )

    fence = coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE
    assert process.u32(fence) == 1
    assert not any(
        address <= fence < address + len(payload)
        for address, payload in process.writes
    )
