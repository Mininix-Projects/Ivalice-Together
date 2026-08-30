import struct
import inspect
from types import SimpleNamespace
from unittest import mock

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

import headless_menu_shell as live
from headless_menu_model import MenuMode, TargetCandidate, TargetGroup


class MemoryProcess:
    def __init__(self):
        self.memory = {}
        self.writes = []

    def seed(self, address, payload):
        for offset, value in enumerate(bytes(payload)):
            self.memory[int(address) + offset] = value

    def read(self, address, size):
        return bytes(self.memory.get(int(address) + offset, 0) for offset in range(size))

    def write(self, address, payload):
        payload = bytes(payload)
        self.writes.append((int(address), payload))
        self.seed(address, payload)

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]


def _listing(payload, address):
    return list(Cs(CS_ARCH_X86, CS_MODE_64).disasm(payload, address))


def _generation_accesses(instructions):
    """Return the full-width reads of the immutable-policy commit word."""

    needle = f"+ {live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET:#x}]"
    return [
        item
        for item in instructions
        if needle in item.op_str
        and "dword ptr" in item.op_str
        and item.mnemonic in {"mov", "cmp"}
    ]


def test_policy_generation_forces_equal_route_commits_and_wraps_nonzero():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)

    bridge.publish_contract(force=True)
    assert bridge.policy_generation == 3
    assert process.u32(bridge.input_state + live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET) == 3

    bridge.publish_contract(force=True)
    assert bridge.policy_generation == 4
    assert bridge.policy_index == 0

    bridge.policy_generation = 0xFFFFFFFF
    bridge.policy_index = 1
    bridge.publish_contract(force=True)
    assert bridge.policy_generation == 2
    assert bridge.policy_index == 0


def test_event_publisher_is_nonblocking_serialized_and_commits_producer_last():
    bridge = live.NativeDialogInputBridge(MemoryProcess(), 0x500000)
    instructions = _listing(
        bridge._build_event_publisher_cave(),
        bridge.event_publisher_cave,
    )
    listing = [(item.mnemonic, item.op_str) for item in instructions]

    assert sum(item.mnemonic == "lock cmpxchg" for item in instructions) == 1
    lock = next(item for item in instructions if item.mnemonic == "lock cmpxchg")
    assert "[r10 + 0x3c]" in lock.op_str
    assert not any(
        item.mnemonic.startswith("j") and int(item.op_str, 16) < item.address
        for item in instructions
    )

    release = next(
        item
        for item in instructions
        if item.mnemonic == "xchg" and "[r10 + 0x3c]" in item.op_str
    )
    acquired_branches = [
        item
        for item in instructions
        if item.mnemonic == "jmp" and item.address > lock.address
    ]
    assert acquired_branches
    release_prep = instructions[instructions.index(release) - 1]
    assert (release_prep.mnemonic, release_prep.op_str) == ("xor", "ecx, ecx")
    assert all(
        int(item.op_str, 16) == release_prep.address for item in acquired_branches
    )

    generation_store = listing.index(("mov", "dword ptr [rcx + 4], r9d"))
    payload_store = listing.index(("mov", "dword ptr [rcx + 8], r8d"))
    sequence_store = listing.index(("mov", "dword ptr [rcx], r11d"))
    producer_commit = listing.index(("xchg", "dword ptr [r10 + 0x24], r11d"))
    assert generation_store < payload_store < sequence_store < producer_commit

    # Every accepted record carries the caller's exact generation.  A refused
    # wheel publication is consumed and dropped (never restored for replay),
    # while RDX/R9 still remain caller-owned across the custom leaf ABI.
    assert not any(
        item.op_str.split(",", 1)[0] in {"rdx", "edx", "r9", "r9d"}
        for item in instructions
        if item.mnemonic not in {"cmp"}
    )


def test_every_policy_hook_double_reads_the_full_generation():
    bridge = live.NativeDialogInputBridge(MemoryProcess(), 0x500000)
    caves = (
        (bridge._build_cave(), bridge.cave),
        (bridge._build_axis_cave(), bridge.axis_cave),
        (bridge._build_command_helper_cave(), bridge.command_helper_cave),
        (bridge._build_desktop_wheel_cave(), bridge.desktop_wheel_cave),
        (bridge._build_mouse_look_cave(), bridge.mouse_look_cave),
    )
    for cave, address in caves:
        accesses = _generation_accesses(_listing(cave, address))
        assert len(accesses) == 2
        assert accesses[0].mnemonic == "mov"
        assert accesses[1].mnemonic == "cmp"
        assert accesses[0].address < accesses[1].address


def test_command_and_wheel_calls_obey_the_publisher_custom_abi():
    bridge = live.NativeDialogInputBridge(MemoryProcess(), 0x500000)

    command = _listing(
        bridge._build_command_helper_cave(), bridge.command_helper_cave
    )
    command_call = next(item for item in command if item.mnemonic == "call")
    command_index = command.index(command_call)
    assert [(item.mnemonic, item.op_str) for item in command[command_index - 3:command_index]] == [
        ("mov", "r9d, ebx"),
        ("push", "rdx"),
        ("push", "r8"),
    ]
    assert [(item.mnemonic, item.op_str) for item in command[command_index + 1:command_index + 3]] == [
        ("pop", "r8"),
        ("pop", "rdx"),
    ]
    assert [item.op_str for item in command[:3]] == ["rbx", "rsi", "rdi"]
    assert [item.op_str for item in command[-4:-1]] == ["rdi", "rsi", "rbx"]

    wheel = _listing(bridge._build_desktop_wheel_cave(), bridge.desktop_wheel_cave)
    wheel_call = next(item for item in wheel if item.mnemonic == "call")
    wheel_index = wheel.index(wheel_call)
    assert [(item.mnemonic, item.op_str) for item in wheel[wheel_index - 3:wheel_index]] == [
        ("mov", "r9d, r11d"),
        ("push", "rdx"),
        ("push", "rcx"),
    ]
    assert [(item.mnemonic, item.op_str) for item in wheel[wheel_index + 1:wheel_index + 3]] == [
        ("pop", "rcx"),
        ("pop", "rdx"),
    ]
    # Wheel events are transient.  A busy/stale/full ring drops the already
    # consumed sample; it must never put the delta back for replay under a
    # later policy generation or after the cooldown expires.
    assert not any(item.mnemonic == "lock add" for item in wheel)


def test_command_source_acquisition_is_one_bounded_cas_with_atomic_clears():
    bridge = live.NativeDialogInputBridge(MemoryProcess(), 0x500000)
    instructions = _listing(
        bridge._build_command_helper_cave(), bridge.command_helper_cave
    )

    acquisitions = [
        item
        for item in instructions
        if item.mnemonic == "lock cmpxchg" and "[r10 + 0x30]" in item.op_str
    ]
    assert len(acquisitions) == 1
    acquisition = acquisitions[0]
    assert acquisition.op_str == "dword ptr [r10 + 0x30], ecx"
    assert not any(
        item.mnemonic.startswith("j") and int(item.op_str, 16) < item.address
        for item in instructions
    )

    # Release/disabled paths must not overwrite a bit concurrently acquired by
    # another alias.  They clear only their own source with an interlocked AND.
    source_clears = [
        item
        for item in instructions
        if item.mnemonic == "lock and" and "[r10 + 0x30]" in item.op_str
    ]
    assert len(source_clears) == 2

    # If the first aggregate publisher refuses the event, clear all sources in
    # one atomic XCHG.  A second alias may already have joined and skipped its
    # own publication; clearing only the first bit would strand that held alias.
    publisher_call = next(item for item in instructions if item.mnemonic == "call")
    rollback = next(
        item
        for item in instructions
        if item.mnemonic == "xchg" and "[r10 + 0x30]" in item.op_str
    )
    assert rollback.op_str == "dword ptr [r10 + 0x30], eax"
    assert rollback.address > publisher_call.address


def test_failed_first_source_publication_rearms_concurrent_joiner():
    # Deterministic interleaving covered by the aggregate-reset contract:
    # A wins 0->A, B joins A->A|B and correctly does not publish, then A's
    # nonblocking publisher refuses.  Resetting the aggregate lets held B retry
    # as the next zero->B edge; clearing only A would permanently lose it.
    source_a = 1
    source_b = 2
    aggregate = 0
    aggregate |= source_a
    aggregate |= source_b
    assert aggregate == source_a | source_b
    aggregate = 0  # atomic XCHG rollback after the refused publication
    previous = aggregate
    aggregate |= source_b
    assert previous == 0
    assert aggregate == source_b


def test_mouse_look_hook_zeros_only_xy_and_replays_the_exact_original_call():
    bridge = live.NativeDialogInputBridge(MemoryProcess(), 0x500000)
    instructions = _listing(bridge._build_mouse_look_cave(), bridge.mouse_look_cave)
    writes = [item for item in instructions if item.mnemonic == "mov" and "[rbx" in item.op_str]

    assert [(item.mnemonic, item.op_str) for item in writes] == [
        ("mov", "qword ptr [rbx + 0x1e8], 0")
    ]
    assert not any("0x1f0" in item.op_str for item in instructions)
    calls = [item for item in instructions if item.mnemonic == "call"]
    assert len(calls) == 1
    assert int(calls[0].op_str, 16) == live.NATIVE_MOUSE_LOOK_FINALIZER
    assert instructions[-1].mnemonic == "jmp"
    assert int(instructions[-1].op_str, 16) == live.NATIVE_MOUSE_LOOK_RETURN


def test_stale_generation_events_are_drained_but_never_routed_or_retained():
    stale = live.NativeDesktopInputEvent(
        1,
        live.NATIVE_INPUT_EVENT_COMMAND,
        1,
        8,
    )
    current = live.NativeDesktopInputEvent(
        2,
        live.NATIVE_INPUT_EVENT_COMMAND,
        1,
        9,
    )
    bridge = SimpleNamespace(
        drain_desktop_events=mock.Mock(return_value=(stale, current)),
        event_overflow_count=0,
        committed_policy_generation=mock.Mock(return_value=9),
    )
    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.native_dialog_bridge = bridge
    runtime.native_desktop_capture_owner = "p2"
    runtime.native_desktop_pending_events = [("old", 1, 1)]
    runtime.native_desktop_overflow_reported = 0
    runtime.frame_subsystem_failures = {}

    accepted = runtime._collect_native_desktop_events(
        foreground=True,
        desktop_owner="p2",
        expected_generation=9,
    )

    assert accepted == (current,)
    assert runtime.native_desktop_pending_events == []


def test_wheel_samples_are_exact_and_always_one_gesture():
    assert live.native_wheel_directions(1) == ()
    assert live.native_wheel_directions(0xFE) == ()
    assert live.native_wheel_directions(0xFF) == (-1,)
    assert live.native_wheel_directions(0x1FE) == (-1,)
    assert live.native_wheel_directions(-0x1FE) == (1,)
    assert live.native_wheel_directions(0xFF * 100000) == (-1,)


def test_wheel_gate_discards_bursts_and_never_replays_them() -> None:
    gate = live.DesktopWheelGestureGate()

    assert gate.consume(0x1FE, now=10.0) == -1
    assert gate.consume(-0xFF, now=10.0) is None
    assert gate.consume(0xFF, now=10.49) is None
    assert gate.consume(-0xFF, now=10.50) == 1
    assert gate.consume(0xFF, now=10.50) is None


def test_wheel_gate_is_wall_clock_stable_at_every_supported_frame_cadence() -> None:
    for fps in (30, 60, 100, 240):
        gate = live.DesktopWheelGestureGate()
        accepted = [
            frame / fps
            for frame in range(fps + 1)
            if gate.consume(0xFF, now=frame / fps) is not None
        ]
        assert accepted == [0.0, 0.5, 1.0]


def test_forced_internal_policy_commit_does_not_rearm_desktop_seeds() -> None:
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    command_seed = bridge.input_state + live.NATIVE_INPUT_COMMAND_SEED_OFFSET
    wheel_seed = bridge.input_state + live.NATIVE_INPUT_WHEEL_SEED_OFFSET

    bridge.publish_contract(
        capture_desktop_events=True,
        desktop_capture_epoch=("p2", 7),
    )
    assert process.u32(command_seed) == 3
    assert process.u32(wheel_seed) == 1

    process.writes.clear()
    bridge.publish_contract(
        capture_desktop_events=True,
        desktop_capture_epoch=("p2", 7),
        force=True,
    )
    assert not any(address in (command_seed, wheel_seed) for address, _ in process.writes)

    process.writes.clear()
    bridge.set_party_menu_blocked(True)
    assert bridge.desktop_capture_epoch == ("p2", 7)
    bridge.publish_contract(
        desktop_capture_epoch=("p2", 7),
        force=True,
    )
    assert not any(address in (command_seed, wheel_seed) for address, _ in process.writes)

    process.writes.clear()
    bridge.publish_contract(
        capture_desktop_events=True,
        desktop_capture_epoch=("p3", 8),
        force=True,
    )
    assert any(address == command_seed for address, _ in process.writes)
    assert any(address == wheel_seed for address, _ in process.writes)


def test_native_leader_suppression_survives_open_and_transition_states():
    for menus_closed, transitions_clear, handoff_pending in (
        (False, True, False),
        (True, False, False),
        (False, False, True),
    ):
        selector, suppression = live.native_leader_replacement_state(
            base_eligible=True,
            menus_closed=menus_closed,
            transitions_clear=transitions_clear,
            handoff_pending=handoff_pending,
        )
        assert not selector
        assert suppression


def test_bootstrap_contract_arms_the_leader_firewall_before_first_snapshot():
    source = inspect.getsource(live.CoopMenuRuntime._initialize)
    bootstrap = source[
        source.index("self.native_dialog_bridge.publish_contract(") : source.index(
            "recovered_tick ="
        )
    ]
    assert "p1_replacement=True" in bootstrap
    assert "non_p1_command_blocked=True" in bootstrap
    assert "leader_replacement=True" in bootstrap
    # Initialization has no coherent live session snapshot yet.  It must not
    # relax the fail-closed contract before the first input frame publishes
    # the real native-frontend exception policy.
    assert source.count("self.native_dialog_bridge.publish_contract(") == 1


def test_dead_leader_waiter_keeps_the_native_direction_firewall_armed():
    source = inspect.getsource(live.CoopMenuRuntime.input)
    leader_policy = source[
        source.index("leader_replacement_base = bool(") : source.index(
            "leader_selector_eligible, leader_replacement_policy"
        )
    ]
    assert "leader_selection_pending" in leader_policy
    assert "or (" in leader_policy
    assert "and not leader_selection_pending" not in leader_policy


def test_dead_leader_pending_arms_follower_command_and_leader_direction_firewalls():
    # UCOFF normally hands COMMAND back to native code, but it is also the
    # transient state in which FF12 used to open its dead-leader selector.  KO
    # recovery therefore overrides passthrough until the mailbox handoff lands.
    non_p1_blocked = live.non_p1_command_firewall_state(
        coop_active=True,
        bridge_ownership_allowed=False,
        native_command_passthrough=True,
        leader_selection_pending=True,
    )
    selector, leader_suppressed = live.native_leader_replacement_state(
        base_eligible=True,
        menus_closed=True,
        transitions_clear=True,
        handoff_pending=True,
    )
    policy = live.native_input_policy(
        p1_replacement=False,
        non_p1_command_blocked=non_p1_blocked,
        custom_directional_owner=False,
        p1_menu_owns_lb=False,
        party_menu_blocked=False,
        quickening_block_mask=0,
        leader_replacement=leader_suppressed,
        capture_desktop_events=False,
        suppress_mouse_look=False,
        p1_native_slot=0,
    )

    assert not selector
    assert policy.other_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert policy.p1_semantic_mask & live.NATIVE_INPUT_DIRECTION_MASK
    assert policy.other_semantic_mask & live.NATIVE_INPUT_DIRECTION_MASK
    assert policy.flags & live.NATIVE_INPUT_POLICY_SUPPRESS_WHEEL

    # Outside KO recovery the explicit native passthrough remains unchanged.
    assert not live.non_p1_command_firewall_state(
        coop_active=True,
        bridge_ownership_allowed=True,
        native_command_passthrough=True,
        leader_selection_pending=False,
    )


def test_dead_leader_pending_masks_p1_even_after_forbidden_native_menu_settles():
    ordinary = live.p1_command_mask_enabled(
        True,
        native_menu_open=True,
        native_command_open=False,
        close_guard_active=False,
        native_menu_settled=True,
    )
    assert not ordinary
    forced = live.p1_command_firewall_state(
        ordinary,
        coop_active=True,
        leader_selection_pending=True,
    )
    policy = live.native_input_policy(
        p1_replacement=forced,
        non_p1_command_blocked=False,
        custom_directional_owner=False,
        p1_menu_owns_lb=False,
        party_menu_blocked=False,
        quickening_block_mask=0,
        leader_replacement=False,
        capture_desktop_events=False,
        p1_native_slot=0,
    )
    expected = live.native_semantic_mask(
        live.NATIVE_DIALOG_COMMAND,
        live.NATIVE_DIALOG_CONFIRM,
        live.NATIVE_DIALOG_CANCEL,
    )
    assert policy.p1_semantic_mask & expected == expected

    # The ordinary settled native frontend retains passthrough outside KO.
    assert not live.p1_command_firewall_state(
        ordinary,
        coop_active=True,
        leader_selection_pending=False,
    )


def test_closed_model_with_stale_opening_transition_fails_closed_before_render():
    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.p1_last_paint = 0.0
    runtime.p1_visual_replacement = True
    runtime.p1_model = SimpleNamespace(mode=MenuMode.CLOSED)
    runtime.p1_transition_kind = "opening"
    runtime.reset_p1_visual_state = mock.Mock()
    runtime.p1_controller = SimpleNamespace(
        presenter=SimpleNamespace(render=mock.Mock(side_effect=AssertionError("rendered")))
    )

    runtime.paint_p1_custom_menu(True)

    runtime.reset_p1_visual_state.assert_called_once_with()
    runtime.p1_controller.presenter.render.assert_not_called()


def test_selector_candidates_ignore_transient_execution_animation_bits():
    model = SimpleNamespace(
        pending_action=live.P1_CHANGE_LEADER_ACTION,
        preserve_candidate_on_refresh=False,
    )
    actor = 0x4000
    battle_keep = 0x5000

    class Process:
        def u64(self, address):
            return {
                actor + 0x698: battle_keep,
                actor: live.EXECUTION_ANIMATION_FLAG,
            }.get(address, 0)

        def u32(self, address):
            return 100 if address == battle_keep + 0x48 else 0

    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.process = Process()
    runtime.p1_model = model
    runtime.model = object()
    runtime.p3_model = object()
    runtime.active_party = [(1, "Ashe", 0x100008, actor)]
    runtime.refresh_actors = mock.Mock()
    runtime.session_snapshot = SimpleNamespace(
        p1_owner_party_member_id=0,
        p1_reserved_party_member_id=0,
        identity=SimpleNamespace(p1_party_member_id=0),
    )

    with mock.patch.object(live, "combined_status_effects", return_value=0):
        candidates = runtime.target_provider(model)

    assert len(candidates) == 1
    assert candidates[0].selectable


def test_leader_selector_anchors_to_one_live_native_leader_sample():
    candidates = tuple(
        TargetCandidate(
            0x100000 + member_id * 8,
            name,
            TargetGroup.PARTY,
            party_member_id=member_id,
        )
        for member_id, name in enumerate(("Vaan", "Balthier", "Ashe"))
    )
    model = SimpleNamespace(
        mode=MenuMode.CLOSED,
        stack=[object()],
        pending_action=None,
        target_group=TargetGroup.FOES,
        target_index=0,
        valid_candidates=mock.Mock(return_value=candidates),
    )
    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.process = object()
    runtime.p1_model = model
    runtime.p1_controller = SimpleNamespace(refresh_targets=mock.Mock())
    runtime.session_snapshot = SimpleNamespace(leader_party_member_id=0)
    runtime.refresh_actors = mock.Mock()
    runtime.begin_p1_opening = mock.Mock()
    runtime.queue_native_sound = mock.Mock()

    with mock.patch.object(live.legacy.coop, "native_party_leader", return_value=1):
        runtime.open_p1_leader_selector()

    assert model.mode is MenuMode.TARGET
    assert model.target_index == 1
    runtime.begin_p1_opening.assert_called_once_with()
