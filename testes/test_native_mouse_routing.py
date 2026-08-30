import builtins
import struct
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from capstone import CS_ARCH_X86, CS_MODE_64, Cs
import pytest

import headless_menu_shell as live
from headless_frontend_controller import InputCommand
from headless_menu_model import MenuMode


class MemoryProcess:
    def __init__(self):
        self.memory = {}
        self.writes = []
        self.patches = []
        self.events = []

    def seed(self, address, data):
        for offset, value in enumerate(bytes(data)):
            self.memory[address + offset] = value

    def read(self, address, size):
        return bytes(self.memory.get(address + offset, 0) for offset in range(size))

    def write(self, address, data):
        payload = bytes(data)
        self.writes.append((address, payload))
        self.events.append(("write", address, payload))
        self.seed(address, payload)

    def patch_code(self, address, data):
        payload = bytes(data)
        self.patches.append((address, payload))
        self.events.append(("patch", address, payload))
        self.seed(address, payload)

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]


class RestoreFailureProcess(MemoryProcess):
    def __init__(self):
        super().__init__()
        self.fail_write_addresses = set()

    def write(self, address, data):
        if address in self.fail_write_addresses:
            raise OSError(f"simulated state failure at {address:#x}")
        super().write(address, data)


class FailOnceWriteProcess(MemoryProcess):
    def __init__(self, fail_address):
        super().__init__()
        self.fail_address = int(fail_address)
        self.failure_injected = False
        self.write_attempts = []

    def write(self, address, data):
        self.write_attempts.append(int(address))
        if int(address) == self.fail_address and not self.failure_injected:
            self.failure_injected = True
            raise OSError(f"simulated one-shot failure at {address:#x}")
        super().write(address, data)


class RootPublicationFailureProcess(MemoryProcess):
    def __init__(self, fail_site, *, fail_after_write):
        super().__init__()
        self.fail_site = fail_site
        self.fail_after_write = bool(fail_after_write)
        self.failure_injected = False

    def patch_code(self, address, data):
        if address != self.fail_site or self.failure_injected:
            super().patch_code(address, data)
            return
        self.failure_injected = True
        if self.fail_after_write:
            super().patch_code(address, data)
        raise OSError(f"simulated root publication failure at {address:#x}")


class CavePublicationFailureProcess(MemoryProcess):
    def __init__(self, fail_address, *, fail_after_write):
        super().__init__()
        self.fail_address = fail_address
        self.fail_after_write = bool(fail_after_write)
        self.failure_injected = False

    def write(self, address, data):
        if address != self.fail_address or self.failure_injected:
            super().write(address, data)
            return
        self.failure_injected = True
        if self.fail_after_write:
            super().write(address, data)
        raise OSError(f"simulated cave publication failure at {address:#x}")


def _payload(kind, value):
    return (kind << live.NATIVE_INPUT_EVENT_KIND_SHIFT) | (
        int(value) & live.NATIVE_INPUT_EVENT_VALUE_MASK
    )


def _seed_u32(process, address, value):
    process.seed(address, struct.pack("<I", int(value) & 0xFFFFFFFF))


def _seed_event(process, bridge, sequence, kind, value, generation=None):
    index = (int(sequence) - 1) & (live.NATIVE_INPUT_EVENT_CAPACITY - 1)
    if generation is None:
        generation = process.u32(
            bridge.input_state + live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET
        )
        if not generation:
            generation = bridge.policy_generation
    process.seed(
        bridge.input_state
        + live.NATIVE_INPUT_EVENT_RING_OFFSET
        + index * live.NATIVE_INPUT_EVENT_RECORD_SIZE,
        struct.pack(
            "<4I",
            sequence & 0xFFFFFFFF,
            int(generation) & 0xFFFFFFFF,
            _payload(kind, value),
            0,
        ),
    )


def _publish_command_alias_sample(process, bridge, sources, source, down):
    sources, edge = live.native_command_source_transition(
        sources,
        source,
        down,
    )
    if not edge:
        return sources, False
    producer_address = (
        bridge.input_state + live.NATIVE_INPUT_EVENT_PRODUCER_OFFSET
    )
    sequence = (process.u32(producer_address) + 1) & 0xFFFFFFFF
    _seed_event(
        process,
        bridge,
        sequence,
        live.NATIVE_INPUT_EVENT_COMMAND,
        source,
    )
    _seed_u32(process, producer_address, sequence)
    return sources, True


@pytest.fixture
def desktop_input_runtime(tmp_path):
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    config_path = Path(tmp_path) / "controller_config.json"
    config = live.control_bindings.default_config()
    config["devices"] = {"p1": None, "p2": None, "p3": None}
    live.control_bindings.set_desktop_owner(config, "p2")
    live.control_bindings.save_config(config, config_path)
    bindings = live.control_bindings.BindingPoller(config_path)

    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.process = process
    runtime.native_dialog_bridge = bridge
    runtime.bindings = bindings
    runtime.role_generation = 1
    runtime.coop_runtime_active = True
    runtime.custom_ui_suppressed = False
    runtime.native_quickening_active = False
    runtime.native_frontend_custom_suspended = False
    runtime.native_frontend_suspended_since = 0.0
    runtime.native_frontend_observed_open = False
    runtime.native_frontend_open_candidate_since = None
    runtime.native_frontend_close_candidate_since = 0.0
    runtime.native_frontend_suspension_source = ""
    runtime.native_frontend_suspended_battle_info = 0
    runtime.native_frontend_suspended_role_generation = 0
    runtime.unexpected_native_menu_cancel_latched = False
    runtime.native_party_menu_active = False
    runtime.native_party_menu_request_pending = False
    runtime.p1_visual_replacement = False
    runtime.p1_pause_suspended = False
    runtime.p2_manual_control_enabled = True
    runtime.p3_manual_control_enabled = False
    runtime.party = [
        ("Vaan", 0, 0x1000),
        ("Penelo", 1, 0x2000),
    ]
    runtime.active_party = [
        (0, "Vaan", 0, 0x1000),
        (1, "Penelo", 1, 0x2000),
    ]
    identity = SimpleNamespace(
        p1_party_member_id=0,
        p2_party_member_id=1,
        p3_party_member_id=None,
    )
    runtime.session_snapshot = SimpleNamespace(
        identity=identity,
        native_menu_open=False,
        native_modal_active=False,
        loading=False,
        cutscene=False,
        chocobo_mode_active=False,
        town=False,
        user_control_enabled=True,
        p1_gameplay_ready=True,
        gameplay_ready=True,
        p1_menu_allowed=True,
        p2_menu_allowed=True,
        bridge_ownership_allowed=False,
        leader_party_member_id=0,
        p1_owner_party_member_id=0,
        p1_is_leader=True,
        battle_info=0,
    )
    runtime.dead_leader_menu_gate = live.DeadLeaderMenuGate()
    runtime.p1_model = SimpleNamespace(mode=MenuMode.CLOSED)
    runtime.model = SimpleNamespace(mode=MenuMode.CLOSED)
    runtime.p3_model = SimpleNamespace(mode=MenuMode.CLOSED)
    runtime.p1_transition_kind = None
    runtime.transition_kind = None
    runtime.p3_transition_kind = None
    runtime.p1_menu_reopen_allowed_at = 0.0
    runtime.menu_reopen_allowed_at = 0.0
    runtime.p3_menu_reopen_allowed_at = 0.0
    runtime.p1_confirm_reopen_guard = live.control_bindings.ConfirmReopenGuard(0.0)
    runtime.p2_confirm_reopen_guard = live.control_bindings.ConfirmReopenGuard(0.0)
    runtime.p3_confirm_reopen_guard = live.control_bindings.ConfirmReopenGuard(0.0)
    runtime.p2_character_direction_guard = (
        live.control_bindings.ConfirmReopenGuard(0.0)
    )
    runtime.p3_character_direction_guard = (
        live.control_bindings.ConfirmReopenGuard(0.0)
    )
    runtime.custom_directional_release_guard = live.DirectionalReleaseGuard(0.0)
    runtime.native_decide_button = live.NATIVE_DECIDE_BUTTON_CROSS
    runtime.native_decide_button_refresh_at = float("inf")
    runtime.p1_native_menu_open_since = None
    runtime.p1_frontend_deferred_open_until = 0.0
    runtime.p1_leader_handoff_sequence = 0
    runtime.native_desktop_capture_owner = None
    runtime.native_desktop_pending_events = deque()
    runtime.native_desktop_overflow_reported = 0

    presenter = SimpleNamespace(set_target_prompts=mock.Mock())
    runtime.p1_controller = SimpleNamespace(presenter=presenter)
    runtime.controller = SimpleNamespace(presenter=presenter)
    runtime.p3_controller = SimpleNamespace(presenter=presenter)
    runtime.native_wait_mode_bridge = SimpleNamespace(
        owns_native_presentation=lambda: False,
        maintain=mock.Mock(),
    )
    runtime.party_edit_capture = SimpleNamespace(begin=mock.Mock())
    runtime.native_interaction_router = SimpleNamespace(
        active=False,
        trigger_atomic_follower_npc=mock.Mock(return_value=False),
    )

    runtime.maintain_p1_leader_handoff = mock.Mock()
    runtime.hard_close_p1_menu = mock.Mock()
    runtime.hard_close_p2_menu = mock.Mock()
    runtime.hard_close_p3_menu = mock.Mock()
    runtime.cancel_native_party_menu_request = mock.Mock()
    runtime.suspend_custom_menus_for_native_frontend = mock.Mock()
    runtime.request_native_party_menu = mock.Mock()
    runtime.maintain_native_party_menu_request = mock.Mock(return_value=False)
    runtime.p1_native_lifecycle = mock.Mock(return_value=None)
    runtime._route_native_quickening_input = mock.Mock(return_value=False)
    runtime._route_native_frontend_input = mock.Mock(return_value=False)
    runtime._handle_p1_battle_input = mock.Mock(return_value=False)
    runtime._handle_p3_battle_input = mock.Mock(return_value=False)
    runtime._world_interactions = mock.Mock(
        return_value=live.WorldInteractions(None, None, None, False, False, False)
    )
    runtime.p2_character_selector_active = mock.Mock(return_value=False)
    runtime.open_p2_character_selector = mock.Mock(
        side_effect=lambda: setattr(runtime.model, "mode", MenuMode.TARGET)
    )
    runtime.dispatch = mock.Mock(
        side_effect=lambda command: (
            setattr(runtime.model, "mode", MenuMode.LIST)
            if command is InputCommand.TOGGLE
            else None
        )
    )

    observed_p2_inputs = []

    def observe_p2_input(**kwargs):
        observed_p2_inputs.append(kwargs["controls"])
        return live.CoopMenuRuntime._handle_p2_battle_input(runtime, **kwargs)

    runtime._handle_p2_battle_input = observe_p2_input
    with (
        mock.patch.object(
            live.control_bindings,
            "read_xinput_states",
            return_value=[None] * 4,
        ),
        mock.patch.object(
            live.control_bindings._user32,
            "GetAsyncKeyState",
            return_value=0,
        ),
        mock.patch.object(
            live.legacy.coop,
            "read_manual_control_mask",
            return_value=live.legacy.coop.P2_MANUAL_CONTROL_BIT,
        ),
        mock.patch.object(
            live,
            "controlled_actor_alive",
            side_effect=lambda _process, actor: True if actor else None,
        ),
        mock.patch.object(
            live,
            "leader_control_availability",
            side_effect=lambda _process, actor: True if actor else None,
        ),
    ):
        yield runtime, bridge, process, observed_p2_inputs
    bindings._controller_channel.close()


def _native_input_patches(bridge):
    return {
        live.NATIVE_GET_BUTTON_SITE: (
            b"\xe9"
            + bridge._rel32(live.NATIVE_GET_BUTTON_SITE, 5, bridge.cave)
            + b"\x90"
        ),
        live.NATIVE_GET_AXIS_SITE: (
            b"\xe9" + bridge._rel32(live.NATIVE_GET_AXIS_SITE, 5, bridge.axis_cave)
        ),
        live.NATIVE_DESKTOP_BUTTON_SITE: (
            b"\xe9"
            + bridge._rel32(
                live.NATIVE_DESKTOP_BUTTON_SITE,
                5,
                bridge.desktop_button_cave,
            )
            + b"\x90" * 3
        ),
        live.NATIVE_DESKTOP_WHEEL_SITE: (
            b"\xe9"
            + bridge._rel32(
                live.NATIVE_DESKTOP_WHEEL_SITE,
                5,
                bridge.desktop_wheel_cave,
            )
            + b"\x90" * 3
        ),
        live.NATIVE_MOUSE_LOOK_SITE: (
            b"\xe9"
            + bridge._rel32(
                live.NATIVE_MOUSE_LOOK_SITE,
                5,
                bridge.mouse_look_cave,
            )
            + b"\x90" * 3
        ),
    }


def test_native_mouse_and_wheel_hooks_use_verified_executable_bytes():
    assert live.NATIVE_DESKTOP_BUTTON_SITE == 0x79D65E
    assert live.NATIVE_DESKTOP_BUTTON_ORIGINAL == bytes.fromhex(
        "0F B6 84 0A F4 01 00 00"
    )
    assert live.NATIVE_GET_BUTTON_SITE == 0x79D670
    assert live.NATIVE_GET_BUTTON_ORIGINAL == bytes.fromhex(
        "8D 42 F5 83 F8 10"
    )
    assert live.NATIVE_DESKTOP_WHEEL_SITE == 0x79D6E6
    assert live.NATIVE_DESKTOP_WHEEL_ORIGINAL == bytes.fromhex(
        "66 0F 6E 81 F0 01 00 00"
    )
    assert live.NATIVE_MOUSE_LOOK_SITE == 0x7A1A1E
    assert live.NATIVE_MOUSE_LOOK_ORIGINAL == bytes.fromhex(
        "48 8B CB E8 7A FB FF FF"
    )


def _native_input_originals():
    return {
        live.NATIVE_GET_BUTTON_SITE: live.NATIVE_GET_BUTTON_ORIGINAL,
        live.NATIVE_GET_AXIS_SITE: live.NATIVE_GET_AXIS_ORIGINAL,
        live.NATIVE_DESKTOP_BUTTON_SITE: live.NATIVE_DESKTOP_BUTTON_ORIGINAL,
        live.NATIVE_DESKTOP_WHEEL_SITE: live.NATIVE_DESKTOP_WHEEL_ORIGINAL,
        live.NATIVE_MOUSE_LOOK_SITE: live.NATIVE_MOUSE_LOOK_ORIGINAL,
    }


def _seed_native_input_layout(process, bridge):
    for _name, address, image in bridge._static_cave_images():
        process.seed(address, image)
    state_image, input_image, _policy = bridge._default_state_images()
    process.seed(bridge.state, state_image)
    process.seed(bridge.input_state, input_image)


def test_install_publishes_complete_caves_before_roots_and_keeps_them_resident():
    process = MemoryProcess()
    originals = _native_input_originals()
    for address, payload in originals.items():
        process.seed(address, payload)
    focus_gate = 0x7A30BB
    process.seed(focus_gate, b"\x75\x1e")

    bridge = live.NativeDialogInputBridge(process, 0x500000)
    bridge.install()

    assert {address for address, _payload_bytes in process.patches} == set(originals)
    assert all(address != focus_gate for address, _payload_bytes in process.patches)
    assert process.read(focus_gate, 2) == b"\x75\x1e"
    expected_wheel_patch = (
        b"\xe9"
        + bridge._rel32(
            live.NATIVE_DESKTOP_WHEEL_SITE,
            5,
            bridge.desktop_wheel_cave,
        )
        + b"\x90" * 3
    )
    assert process.read(live.NATIVE_DESKTOP_WHEEL_SITE, 8) == expected_wheel_patch

    first_patch = next(
        index for index, event in enumerate(process.events) if event[0] == "patch"
    )
    cave_writes = {
        (address, payload)
        for kind, address, payload in process.events[:first_patch]
        if kind == "write"
    }
    assert {
        (address, image)
        for _name, address, image in bridge._static_cave_images()
    }.issubset(cave_writes)

    patches = _native_input_patches(bridge)
    cave_images = {
        address: process.read(address, len(image))
        for _name, address, image in bridge._static_cave_images()
    }
    patch_count = len(process.patches)

    bridge.restore()

    assert not bridge.installed
    assert len(process.patches) == patch_count
    assert {
        address: process.read(address, len(patch))
        for address, patch in patches.items()
    } == patches
    assert {
        address: process.read(address, len(image))
        for _name, address, image in bridge._static_cave_images()
    } == cave_images
    active_index = process.u32(
        bridge.input_state + live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET
    )
    active_policy = live.NativeInputPolicy(
        *struct.unpack_from(
            "<4I",
            process.read(bridge.input_state, live.NATIVE_INPUT_STATE_SIZE),
            active_index * live.NATIVE_INPUT_POLICY_SLOT_SIZE,
        )
    )
    assert active_policy.p1_semantic_mask == 0
    assert active_policy.other_semantic_mask == 0
    assert not (
        active_policy.flags
        & (
            live.NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
            | live.NATIVE_INPUT_POLICY_SUPPRESS_WHEEL
            | live.NATIVE_INPUT_POLICY_SUPPRESS_P1_AXES
        )
    )
    assert process.read(bridge.state, 5) == b"\x00" * 5
    for offset in (
        live.NATIVE_INPUT_COMMAND_SOURCES_OFFSET,
        live.NATIVE_INPUT_COMMAND_SEED_OFFSET,
        live.NATIVE_INPUT_WHEEL_SEED_OFFSET,
    ):
        assert process.read(bridge.input_state + offset, 4) == b"\x00" * 4


def test_restart_adopts_every_exact_hook_without_rewriting_any_cave():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    patches = _native_input_patches(bridge)
    for address, payload in patches.items():
        process.seed(address, payload)
    _seed_native_input_layout(process, bridge)

    adopted_policy = live.NativeInputPolicy(
        live.NATIVE_INPUT_COMMAND_MASK,
        live.NATIVE_INPUT_PARTY_MASK,
        2,
        live.NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
        | live.NATIVE_INPUT_POLICY_SUPPRESS_WHEEL
        | (0x0B << live.NATIVE_INPUT_POLICY_GAMEPAD_SLOT_SHIFT),
    )
    process.seed(
        bridge.input_state + live.NATIVE_INPUT_POLICY_SLOT_SIZE,
        adopted_policy.pack(),
    )
    process.seed(
        bridge.input_state + live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET,
        struct.pack("<I", 3),
    )
    process.seed(
        bridge.state + live.NATIVE_DIALOG_P1_SLOT_OFFSET,
        struct.pack("<I", 2),
    )
    _seed_u32(
        process,
        bridge.input_state + live.NATIVE_INPUT_EVENT_PRODUCER_OFFSET,
        9,
    )
    _seed_u32(
        process,
        bridge.input_state + live.NATIVE_INPUT_EVENT_CONSUMER_OFFSET,
        7,
    )
    _seed_u32(
        process,
        bridge.input_state + live.NATIVE_INPUT_EVENT_OVERFLOW_OFFSET,
        3,
    )

    bridge.install()

    assert process.events == []
    assert bridge.installed
    assert not bridge.owns_static
    assert bridge.policy_index == 1
    assert bridge.policy_generation == 3
    assert bridge.policy == adopted_policy
    assert bridge.p1_controller_slot == 2
    assert bridge.native_gamepad_slot_mask == 0x0B
    assert bridge.event_consumer_sequence == 7
    assert bridge.event_overflow_count == 3
    assert {
        address: process.read(address, len(patch)) for address, patch in patches.items()
    } == patches


def test_restart_cave_mismatch_aborts_without_detaching_or_rewriting():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    patches = _native_input_patches(bridge)
    for address, payload in patches.items():
        process.seed(address, payload)
    _seed_native_input_layout(process, bridge)
    process.seed(bridge.command_helper_cave + 7, b"\xCC")

    with pytest.raises(RuntimeError, match="active native command helper cave mismatch"):
        bridge.install()

    assert not bridge.installed
    assert process.events == []
    assert {
        address: process.read(address, len(patch))
        for address, patch in patches.items()
    } == patches


@pytest.mark.parametrize(
    ("layout_mutation", "error_match"),
    (
        ("magic", "layout magic mismatch"),
        ("active-generation", "policy generation is incoherent"),
        ("event-ring", "event ring is incoherent"),
    ),
)
def test_restart_rejects_incoherent_layout_without_mutation(
    layout_mutation,
    error_match,
):
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    patches = _native_input_patches(bridge)
    for address, payload in patches.items():
        process.seed(address, payload)
    _seed_native_input_layout(process, bridge)

    if layout_mutation == "magic":
        process.seed(
            bridge.state + live.NATIVE_DIALOG_LAYOUT_MAGIC_OFFSET,
            b"\xCC",
        )
    elif layout_mutation == "active-generation":
        process.seed(
            bridge.input_state + live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET,
            struct.pack("<I", 0),
        )
    else:
        _seed_u32(
            process,
            bridge.input_state + live.NATIVE_INPUT_EVENT_PRODUCER_OFFSET,
            live.NATIVE_INPUT_EVENT_CAPACITY + 1,
        )

    with pytest.raises(RuntimeError, match=error_match):
        bridge.install()

    assert not bridge.installed
    assert process.events == []


def test_restart_rejects_legacy_v1_layout_without_mutation():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    for address, payload in _native_input_patches(bridge).items():
        process.seed(address, payload)
    _seed_native_input_layout(process, bridge)
    process.seed(
        bridge.state + live.NATIVE_DIALOG_LAYOUT_MAGIC_OFFSET,
        b"NDIB3V1",
    )

    with pytest.raises(RuntimeError, match="layout magic mismatch"):
        bridge.install()

    assert not bridge.installed
    assert process.events == []


def test_restart_treats_atomic_policy_as_authority_and_repairs_legacy_slot():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    for address, payload in _native_input_patches(bridge).items():
        process.seed(address, payload)
    _seed_native_input_layout(process, bridge)
    process.seed(
        bridge.state + live.NATIVE_DIALOG_P1_SLOT_OFFSET,
        struct.pack("<I", 1),
    )

    bridge.install()

    assert bridge.installed
    assert bridge.policy.p1_native_slot == live.legacy.coop.INVALID_NATIVE_CONTROLLER_SLOT
    assert bridge.compact_p1_controller_slot == 1
    assert bridge.p1_replacement
    assert bridge.non_p1_command_blocked
    assert bridge.leader_replacement
    assert bridge.party_menu_blocked
    # The persistent leader firewall owns only d-pad semantics. Shoulders stay
    # native until an actual custom-menu lifecycle claims them.
    assert not bridge.custom_directional_owner
    assert process.events == []

    # An ordinary idempotent publication repairs the obsolete mirror even
    # though the authoritative policy generation itself needs no new commit.
    bridge.publish_contract()
    assert process.events == [
        (
            "write",
            bridge.state + live.NATIVE_DIALOG_P1_SLOT_OFFSET,
            struct.pack("<I", live.legacy.coop.INVALID_NATIVE_CONTROLLER_SLOT),
        )
    ]


@pytest.mark.parametrize(
    "contract",
    (
        {},
        {
            "p1_replacement": True,
            "non_p1_command_blocked": True,
            "party_menu_blocked": True,
            "leader_replacement": True,
        },
        {
            "custom_directional_owner": True,
            "p1_menu_owns_lb": True,
        },
        {
            "quickening_block_mask": (
                live.QUICKENING_BLOCK_COMMAND | live.QUICKENING_BLOCK_CONFIRM
            ),
        },
        {
            "capture_desktop_events": True,
            "suppress_mouse_look": True,
        },
    ),
)
def test_adopted_policy_hydration_is_an_exact_noop(contract):
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    policy = live.native_input_policy(
        **(
            {
                "p1_replacement": False,
                "non_p1_command_blocked": False,
                "custom_directional_owner": False,
                "p1_menu_owns_lb": False,
                "party_menu_blocked": False,
                "quickening_block_mask": 0,
                "leader_replacement": False,
                "capture_desktop_events": False,
                "suppress_mouse_look": False,
                "p1_native_slot": 2,
                "native_gamepad_slot_mask": 0x0F,
            }
            | contract
        )
    )
    bridge._reset_local_contract(policy, 10)
    bridge.compact_p1_controller_slot = policy.p1_native_slot
    process.events.clear()

    assert bridge.publish_contract() == policy
    assert bridge.policy == policy
    assert process.events == []


def test_adopted_leader_policy_never_grows_missing_shoulders_on_republish():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    policy = live.NativeInputPolicy(
        live.NATIVE_INPUT_DIRECTION_MASK,
        live.NATIVE_INPUT_DIRECTION_MASK,
        live.legacy.coop.INVALID_NATIVE_CONTROLLER_SLOT,
        0,
    )

    bridge._reset_local_contract(policy, 10)
    process.events.clear()

    assert bridge.leader_replacement
    republished = bridge.publish_contract()
    assert not (
        republished.p1_semantic_mask
        & live.native_semantic_mask(live.NATIVE_DIALOG_LB, live.NATIVE_DIALOG_RB)
    )
    assert not (
        republished.other_semantic_mask
        & live.native_semantic_mask(live.NATIVE_DIALOG_LB, live.NATIVE_DIALOG_RB)
    )


def test_adopted_complete_leader_policy_preserves_shoulders_and_is_exact_noop():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    policy = live.native_input_policy(
        p1_replacement=False,
        non_p1_command_blocked=False,
        custom_directional_owner=False,
        p1_menu_owns_lb=False,
        party_menu_blocked=False,
        quickening_block_mask=0,
        leader_replacement=True,
        capture_desktop_events=False,
        p1_native_slot=live.legacy.coop.INVALID_NATIVE_CONTROLLER_SLOT,
    )

    bridge._reset_local_contract(policy, 10)
    process.events.clear()

    assert bridge.leader_replacement
    assert (
        bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_LEADER_MASK
        == live.NATIVE_INPUT_LEADER_MASK
    )
    assert (
        bridge.policy.other_semantic_mask & live.NATIVE_INPUT_LEADER_MASK
        == live.NATIVE_INPUT_LEADER_MASK
    )
    shoulder_mask = live.native_semantic_mask(
        live.NATIVE_DIALOG_LB,
        live.NATIVE_DIALOG_RB,
    )
    assert not bridge.policy.p1_semantic_mask & shoulder_mask
    assert not bridge.policy.other_semantic_mask & shoulder_mask
    assert bridge.publish_contract() == policy
    assert process.events == []


def test_restart_disarms_a_foreign_synthetic_pulse_before_running():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    for address, payload in _native_input_patches(bridge).items():
        process.seed(address, payload)
    _seed_native_input_layout(process, bridge)
    process.seed(
        bridge.state,
        struct.pack("<I", live.NATIVE_DIALOG_PARTY_MENU) + b"\x01",
    )

    bridge.install()

    assert bridge.installed
    assert process.read(bridge.state + 4, 1) == b"\x00"
    assert process.events == [("write", bridge.state + 4, b"\x00")]


def test_failed_legacy_slot_mirror_is_retried_even_when_desired_slot_matches():
    process = RestoreFailureProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    mirror = bridge.state + live.NATIVE_DIALOG_P1_SLOT_OFFSET
    process.fail_write_addresses.add(mirror)

    with pytest.raises(OSError, match="simulated state failure"):
        bridge.set_p1_controller(3)
    assert bridge.p1_controller_slot == 3
    assert bridge.policy.p1_native_slot == live.legacy.coop.INVALID_NATIVE_CONTROLLER_SLOT

    process.fail_write_addresses.clear()
    bridge.set_p1_controller(3)
    assert bridge.compact_p1_controller_slot == 3
    assert bridge.policy.p1_native_slot == 3


def test_foreign_hook_is_rejected_before_restart_mutates_any_site_or_cave():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    originals = {
        live.NATIVE_GET_BUTTON_SITE: live.NATIVE_GET_BUTTON_ORIGINAL,
        live.NATIVE_GET_AXIS_SITE: live.NATIVE_GET_AXIS_ORIGINAL,
        live.NATIVE_DESKTOP_BUTTON_SITE: live.NATIVE_DESKTOP_BUTTON_ORIGINAL,
        live.NATIVE_DESKTOP_WHEEL_SITE: live.NATIVE_DESKTOP_WHEEL_ORIGINAL,
    }
    for address, payload in originals.items():
        process.seed(address, payload)
    process.seed(live.NATIVE_DESKTOP_WHEEL_SITE, b"\xcc" * 8)

    with pytest.raises(RuntimeError, match="native desktop GET_WHEEL occupied"):
        bridge.install()

    assert process.events == []


def test_stock_roots_with_nonzero_reservation_abort_without_any_mutation():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    for address, payload in _native_input_originals().items():
        process.seed(address, payload)
    # The byte is padding, not executable body; the whole reservation belongs
    # to this process-lifetime layout and must therefore be pristine.
    _name, address, image = bridge._static_cave_images()[0]
    process.seed(address + len(image) - 1, b"\xA5")

    with pytest.raises(RuntimeError, match="non-empty .* stock roots"):
        bridge.install()

    assert process.events == []


def test_partial_exact_roots_are_rejected_read_only():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    originals = _native_input_originals()
    patches = _native_input_patches(bridge)
    for address, payload in originals.items():
        process.seed(address, payload)
    _seed_native_input_layout(process, bridge)
    process.seed(live.NATIVE_GET_BUTTON_SITE, patches[live.NATIVE_GET_BUTTON_SITE])
    snapshot = dict(process.memory)

    with pytest.raises(RuntimeError, match="partial .* restart the game"):
        bridge.install()

    assert process.events == []
    assert process.memory == snapshot


@pytest.mark.parametrize("publication_index", range(9))
@pytest.mark.parametrize("fail_after_write", (False, True))
def test_state_and_cave_publication_failure_never_exposes_a_root(
    publication_index,
    fail_after_write,
):
    template = live.NativeDialogInputBridge(MemoryProcess(), 0x500000)
    publication_addresses = (
        template.state,
        template.input_state,
        *(address for _name, address, _image in template._static_cave_images()),
    )
    fail_address = publication_addresses[publication_index]
    process = CavePublicationFailureProcess(
        fail_address,
        fail_after_write=fail_after_write,
    )
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    originals = _native_input_originals()
    for address, payload in originals.items():
        process.seed(address, payload)

    with pytest.raises(RuntimeError, match="cave publication failed; restart"):
        bridge.install()

    assert process.patches == []
    assert {
        address: process.read(address, len(original))
        for address, original in originals.items()
    } == originals

    events_before_retry = list(process.events)
    memory_before_retry = dict(process.memory)
    retry_bridge = live.NativeDialogInputBridge(process, 0x500000)
    if publication_index == 0 and not fail_after_write:
        # Nothing at all was published; a fresh retry remains legitimate.
        retry_bridge.install()
        assert retry_bridge.installed
        assert retry_bridge.owns_static
    else:
        with pytest.raises(RuntimeError, match="non-empty .* stock roots"):
            retry_bridge.install()
        assert process.events == events_before_retry
        assert process.memory == memory_before_retry


@pytest.mark.parametrize(
    "fail_site",
    (
        live.NATIVE_GET_BUTTON_SITE,
        live.NATIVE_GET_AXIS_SITE,
        live.NATIVE_DESKTOP_BUTTON_SITE,
        live.NATIVE_DESKTOP_WHEEL_SITE,
        live.NATIVE_MOUSE_LOOK_SITE,
    ),
)
@pytest.mark.parametrize("fail_after_write", (False, True))
def test_root_publication_failure_is_never_rolled_back_and_requires_restart(
    fail_site,
    fail_after_write,
):
    process = RootPublicationFailureProcess(
        fail_site,
        fail_after_write=fail_after_write,
    )
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    originals = _native_input_originals()
    patches = _native_input_patches(bridge)
    for address, payload in originals.items():
        process.seed(address, payload)

    with pytest.raises(RuntimeError, match="publication failed; restart the game"):
        bridge.install()

    root_order = tuple(patches)
    failing_index = root_order.index(fail_site)
    published_count = failing_index + int(fail_after_write)
    for index, address in enumerate(root_order):
        expected = patches[address] if index < published_count else originals[address]
        assert process.read(address, len(expected)) == expected
    assert not bridge.installed
    assert all(
        payload != originals.get(address)
        for address, payload in process.patches
        if address in originals
    )
    for _name, address, image in bridge._static_cave_images():
        assert process.read(address, len(image)) == image

    events_before_retry = list(process.events)
    memory_before_retry = dict(process.memory)
    retry_bridge = live.NativeDialogInputBridge(process, 0x500000)
    if fail_after_write and fail_site == root_order[-1]:
        # An exception observed after the final write is indistinguishable from
        # a completed resident topology.  Exact preflight may safely adopt it.
        retry_bridge.install()
        assert retry_bridge.installed
        assert not retry_bridge.owns_static
    else:
        with pytest.raises(RuntimeError, match="restart the game"):
            retry_bridge.install()
    assert process.events == events_before_retry
    assert process.memory == memory_before_retry


@pytest.mark.parametrize("failure_kind", ("pulse-state", "policy-commit"))
def test_restore_keeps_static_layout_resident_when_dynamic_cleanup_fails(
    failure_kind,
):
    process = RestoreFailureProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    originals = _native_input_originals()
    for address, payload in originals.items():
        process.seed(address, payload)
    bridge.install()
    patches = _native_input_patches(bridge)
    cave_images = {
        address: process.read(address, len(image))
        for _name, address, image in bridge._static_cave_images()
    }
    patch_count = len(process.patches)

    if failure_kind == "pulse-state":
        process.fail_write_addresses.add(bridge.state + 4)
    else:
        bridge.publish_contract(p1_replacement=True)
        process.fail_write_addresses.add(
            bridge.input_state + live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET
        )

    with pytest.raises(OSError, match="simulated state failure"):
        bridge.restore()

    assert bridge.installed
    assert len(process.patches) == patch_count
    assert {
        address: process.read(address, len(patch))
        for address, patch in patches.items()
    } == patches
    assert {
        address: process.read(address, len(image))
        for _name, address, image in bridge._static_cave_images()
    } == cave_images

    process.fail_write_addresses.clear()
    bridge.restore()
    assert not bridge.installed
    assert {
        address: process.read(address, len(patch))
        for address, patch in patches.items()
    } == patches


@pytest.mark.parametrize("failed_stage", ("policy_slot", "policy_commit"))
def test_restore_retries_one_shot_policy_failure_to_publish_all_native(
    failed_stage,
):
    process = FailOnceWriteProcess(-1)
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    bridge.installed = True
    bridge.publish_contract(
        p1_replacement=True,
        non_p1_command_blocked=True,
        leader_replacement=True,
        capture_desktop_events=True,
        desktop_capture_epoch=("p2", 1),
    )
    next_generation = (bridge.policy_generation + 1) & 0xFFFFFFFF
    if next_generation == 0:
        next_generation = 2
    targets = {
        "policy_slot": (
            bridge.input_state
            + (next_generation & 1) * live.NATIVE_INPUT_POLICY_SLOT_SIZE
        ),
        "policy_commit": (
            bridge.input_state + live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET
        ),
    }
    process.fail_address = targets[failed_stage]

    with pytest.raises(OSError):
        bridge.restore()

    assert process.failure_injected
    assert bridge.policy.p1_semantic_mask == 0
    assert bridge.policy.other_semantic_mask == 0
    committed = process.u32(
        bridge.input_state + live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET
    )
    assert committed == bridge.policy_generation
    assert process.read(
        bridge.input_state
        + (committed & 1) * live.NATIVE_INPUT_POLICY_SLOT_SIZE,
        live.NATIVE_INPUT_POLICY_SLOT_SIZE,
    ) == bridge.policy.pack()


def test_all_native_input_caves_fit_their_non_overlapping_reservations():
    bridge = live.NativeDialogInputBridge(MemoryProcess(), 0x500000)
    assert bridge.axis_cave + len(bridge._build_axis_cave()) <= bridge.command_helper_cave
    assert (
        bridge.command_helper_cave + len(bridge._build_command_helper_cave())
        <= bridge.base + live.P1_LIST_INPUT_CAVE_OFFSET
    )
    assert (
        bridge.base
        + live.P1_LIST_INPUT_CAVE_OFFSET
        + live.P1_LIST_INPUT_CAVE_SIZE
        <= bridge.desktop_button_cave
    )
    assert (
        bridge.desktop_button_cave + len(bridge._build_desktop_button_cave())
        <= bridge.base + live.P1_LIST_INPUT_STATE_OFFSET
    )
    assert bridge.mouse_look_cave + len(bridge._build_mouse_look_cave()) <= (
        bridge.desktop_wheel_cave
    )
    assert bridge.desktop_wheel_cave + len(bridge._build_desktop_wheel_cave()) <= (
        bridge.cave
    )
    assert bridge.cave + len(bridge._build_cave()) <= bridge.state
    assert bridge.event_publisher_cave + len(bridge._build_event_publisher_cave()) <= (
        bridge.base + 0xF000
    )
    assert bridge.input_state + live.NATIVE_INPUT_STATE_SIZE <= bridge.base + 0xE800


def test_axis_cave_reads_slot_and_ownership_from_one_active_policy_generation():
    bridge = live.NativeDialogInputBridge(MemoryProcess(), 0x500000)
    cave = bridge._build_axis_cave()
    instructions = list(Cs(CS_ARCH_X86, CS_MODE_64).disasm(cave, bridge.axis_cave))
    listing = [(instruction.mnemonic, instruction.op_str) for instruction in instructions]

    active_load = listing.index(("mov", "r11d, dword ptr [r10 + 0x20]"))
    stable_read = listing.index(("cmp", "r11d, dword ptr [r10 + 0x20]"))
    slot_load = listing.index(("mov", "r11d, dword ptr [r8 + 8]"))
    slot_match = listing.index(("cmp", "dword ptr [rcx + 0x458], r11d"))
    ownership = listing.index(("test", "dword ptr [r8 + 0xc], 4"))
    assert active_load < stable_read < slot_load < slot_match < ownership
    assert ("cmp", "byte ptr [rax + 5], 0") not in listing


def test_disabling_p1_replacement_restores_axes_in_the_next_atomic_policy():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)

    owned = bridge.publish_contract(p1_replacement=True, p1_controller_slot=2)
    assert owned.flags & live.NATIVE_INPUT_POLICY_SUPPRESS_P1_AXES
    owned_generation = process.u32(
        bridge.input_state + live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET
    )
    owned_bytes = process.read(
        bridge.input_state
        + (owned_generation & 1) * live.NATIVE_INPUT_POLICY_SLOT_SIZE,
        live.NATIVE_INPUT_POLICY_SLOT_SIZE,
    )
    assert struct.unpack("<4I", owned_bytes) == (
        owned.p1_semantic_mask,
        owned.other_semantic_mask,
        2,
        owned.flags,
    )

    native = bridge.publish_contract(p1_replacement=False)
    assert not native.flags & live.NATIVE_INPUT_POLICY_SUPPRESS_P1_AXES
    assert process.read(bridge.state + 5, 1) == b"\x00"


def test_combined_contract_has_one_atomic_commit_and_no_transient_contract():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    policy = bridge.publish_contract(
        p1_replacement=True,
        non_p1_command_blocked=True,
        custom_directional_owner=True,
        p1_menu_owns_lb=True,
        party_menu_blocked=True,
        quickening_block_mask=(
            live.QUICKENING_BLOCK_COMMAND | live.QUICKENING_BLOCK_CONFIRM
        ),
        leader_replacement=True,
        capture_desktop_events=True,
        p1_controller_slot=0,
        native_gamepad_slot_mask=0b0111,
    )

    commit_address = bridge.input_state + live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET
    commits = [write for write in process.writes if write[0] == commit_address]
    assert len(commits) == 1
    assert process.writes.index(commits[0]) == len(process.writes) - 1
    assert policy.flags & live.NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
    assert policy.flags & live.NATIVE_INPUT_POLICY_SUPPRESS_WHEEL
    assert (
        policy.flags & live.NATIVE_INPUT_POLICY_GAMEPAD_SLOT_MASK
        == 0b0111 << live.NATIVE_INPUT_POLICY_GAMEPAD_SLOT_SHIFT
    )

    process.writes.clear()
    bridge.publish_contract(
        p1_replacement=True,
        non_p1_command_blocked=True,
        custom_directional_owner=True,
        p1_menu_owns_lb=True,
        party_menu_blocked=True,
        quickening_block_mask=(
            live.QUICKENING_BLOCK_COMMAND | live.QUICKENING_BLOCK_CONFIRM
        ),
        leader_replacement=True,
        capture_desktop_events=True,
        p1_controller_slot=0,
        native_gamepad_slot_mask=0b0111,
    )
    assert process.writes == []


def test_wheel_policy_captures_for_any_owner_and_suppresses_only_during_ownership():
    bridge = live.NativeDialogInputBridge(MemoryProcess(), 0x500000)

    capture_only = bridge.publish_contract(capture_desktop_events=True)
    assert capture_only.flags & live.NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
    assert capture_only.flags & live.NATIVE_INPUT_POLICY_SUPPRESS_WHEEL
    assert capture_only.other_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK

    all_native = bridge.publish_contract(capture_desktop_events=False)
    assert not all_native.flags & live.NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
    assert not all_native.flags & live.NATIVE_INPUT_POLICY_SUPPRESS_WHEEL

    handoff_suppression = bridge.publish_contract(leader_replacement=True)
    assert not handoff_suppression.flags & live.NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
    assert handoff_suppression.flags & live.NATIVE_INPUT_POLICY_SUPPRESS_WHEEL

    all_native = bridge.publish_contract(leader_replacement=False)
    assert not all_native.flags & live.NATIVE_INPUT_POLICY_SUPPRESS_WHEEL


def test_runtime_combined_publication_calls_only_the_atomic_publisher():
    bridge = SimpleNamespace(
        publish_contract=mock.Mock(return_value=live.NativeInputPolicy()),
        set_p1_replacement=mock.Mock(),
        set_non_p1_command_blocked=mock.Mock(),
        set_custom_directional_owner=mock.Mock(),
        set_p1_menu_owns_lb=mock.Mock(),
        set_party_menu_blocked=mock.Mock(),
        set_quickening_block_mask=mock.Mock(),
        set_leader_replacement=mock.Mock(),
        set_desktop_events_enabled=mock.Mock(),
    )
    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.native_dialog_bridge = bridge
    contract = dict(
        p1_replacement=True,
        non_p1_command_blocked=True,
        custom_directional_owner=True,
        p1_menu_owns_lb=True,
        party_menu_blocked=True,
        quickening_block_mask=3,
        leader_replacement=True,
        capture_desktop_events=True,
        native_gamepad_slot_mask=0b0111,
    )

    runtime._publish_native_input_contract(**contract)

    bridge.publish_contract.assert_called_once_with(**contract)
    for name, value in vars(bridge).items():
        if name.startswith("set_"):
            value.assert_not_called()


def test_normalized_gamepad_command_cannot_enter_the_desktop_ring():
    gamepads = live.native_gamepad_slot_mask(0, 1, 2)
    assert gamepads == 0b1111
    assert not live.native_command_source_is_desktop(2, 0, gamepads)
    assert not live.native_command_source_is_desktop(2, 1, gamepads)
    assert not live.native_command_source_is_desktop(2, 2, gamepads)
    # Slot 3 is deliberately unassigned but may still be connected and polled.
    assert not live.native_command_source_is_desktop(2, 3, gamepads)
    assert live.native_command_source_is_desktop(2, 7, gamepads)
    # The direct 0x79D65E observation is class-proven mouse input even if a
    # device index happens to share a numeric slot with a controller.
    assert live.native_command_source_is_desktop(1, 1, gamepads)

    sources = 0
    if live.native_command_source_is_desktop(2, 1, gamepads):
        sources, _edge = live.native_command_source_transition(sources, 2, True)
    assert sources == 0

    bridge = live.NativeDialogInputBridge(MemoryProcess(), 0x500000)
    cave = bridge._build_cave()
    assert b"\x83\xf8\x03" in cave
    assert b"\x83\xc0\x08\x41\x0f\xa3\x40\x0c\x0f\x82" in cave


def test_alias_interleaving_latches_both_sources_but_publishes_one_edge():
    sources, edge = live.native_command_source_transition(0, 1, True)
    assert (sources, edge) == (1, True)
    sources, edge = live.native_command_source_transition(sources, 2, True)
    assert (sources, edge) == (3, False)
    sources, edge = live.native_command_source_transition(sources, 1, False)
    assert (sources, edge) == (2, False)
    sources, edge = live.native_command_source_transition(sources, 2, True)
    assert (sources, edge) == (2, False)
    sources, edge = live.native_command_source_transition(sources, 2, False)
    assert (sources, edge) == (0, False)
    sources, edge = live.native_command_source_transition(sources, 2, True)
    assert (sources, edge) == (2, True)


def test_foreground_resume_absorbs_held_aliases_until_each_releases():
    sources, seeds = 0, 3
    sources, seeds, edge = live.native_command_capture_sample(
        sources, seeds, 1, True
    )
    assert (sources, seeds, edge) == (0, 3, False)
    sources, seeds, edge = live.native_command_capture_sample(
        sources, seeds, 2, True
    )
    assert (sources, seeds, edge) == (0, 3, False)
    sources, seeds, edge = live.native_command_capture_sample(
        sources, seeds, 1, False
    )
    assert (sources, seeds, edge) == (0, 2, False)
    sources, seeds, edge = live.native_command_capture_sample(
        sources, seeds, 1, True
    )
    assert (sources, seeds, edge) == (1, 2, True)
    sources, seeds, edge = live.native_command_capture_sample(
        sources, seeds, 2, False
    )
    assert (sources, seeds, edge) == (1, 0, False)
    sources, seeds, edge = live.native_command_capture_sample(
        sources, seeds, 2, True
    )
    assert (sources, seeds, edge) == (3, 0, False)

    helper = live.NativeDialogInputBridge(
        MemoryProcess(), 0x500000
    )._build_command_helper_cave()
    instructions = list(Cs(CS_ARCH_X86, CS_MODE_64).disasm(helper, 0x50554E))
    seed_test = next(
        instruction
        for instruction in instructions
        if instruction.mnemonic == "test" and "[r10 + 0x34]" in instruction.op_str
    )
    # R8D is the source bit.  Testing the complete seed DWORD would let an
    # unused alias suppress a newly pressed alias forever after focus resumes.
    assert seed_test.op_str.endswith(", r8d")


def test_full_command_ring_atomically_rearms_the_aggregate_for_retry():
    sources, edge = live.native_command_source_transition(
        0,
        1,
        True,
        publish_succeeded=False,
    )
    assert (sources, edge) == (0, False)
    sources, edge = live.native_command_source_transition(
        sources,
        1,
        True,
        publish_succeeded=True,
    )
    assert (sources, edge) == (1, True)

    helper = live.NativeDialogInputBridge(
        MemoryProcess(), 0x500000
    )._build_command_helper_cave()
    assert b"\x45\x85\xc3" in helper  # test old aggregate against this source
    assert b"\xf0\x41\x0f\xb1\x4a\x30" in helper  # bounded source CAS
    assert b"\x52\x41\x50\xe8" in helper
    assert b"\x41\x58\x5a\x85\xc0\x0f\x85" in helper
    assert b"\x31\xc0\x41\x87\x42\x30" in helper  # aggregate rearm

    # B can join after A wins the zero->A acquisition but before A's publisher
    # refuses.  Resetting only A would strand B as down with no delivered edge.
    sources, edge = live.native_command_source_transition(
        1 | 2,
        1,
        True,
        publish_succeeded=False,
    )
    assert (sources, edge) == (1 | 2, False)  # A was already represented


def test_wheel_sign_and_rapid_alternation_preserve_native_direction():
    assert live.native_wheel_directions(0xFF) == (-1,)
    assert live.native_wheel_directions(-0xFF) == (1,)
    assert live.native_wheel_directions(0x1FE) == (-1,)
    alternating = (0xFF, -0xFF, 0xFF)
    assert tuple(
        direction
        for delta in alternating
        for direction in live.native_wheel_directions(delta)
    ) == (-1, 1, -1)


def test_ring_drains_wheel_alternation_in_order_and_exactly_once():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    producer = bridge.input_state + live.NATIVE_INPUT_EVENT_PRODUCER_OFFSET
    consumer = bridge.input_state + live.NATIVE_INPUT_EVENT_CONSUMER_OFFSET
    overflow = bridge.input_state + live.NATIVE_INPUT_EVENT_OVERFLOW_OFFSET
    _seed_u32(process, producer, 3)
    _seed_u32(process, consumer, 0)
    _seed_u32(process, overflow, 0)
    for sequence, delta in enumerate((0xFF, -0xFF, 0xFF), start=1):
        _seed_event(process, bridge, sequence, live.NATIVE_INPUT_EVENT_WHEEL, delta)

    events = bridge.drain_desktop_events()

    assert [event.sequence for event in events] == [1, 2, 3]
    assert [event.value for event in events] == [0xFF, -0xFF, 0xFF]
    assert tuple(
        direction
        for event in events
        for direction in live.native_wheel_directions(event.value)
    ) == (-1, 1, -1)
    assert bridge.drain_desktop_events() == ()


def test_m1_alias_ring_routes_one_confirm_to_closed_then_open_menu(
    desktop_input_runtime,
):
    runtime, bridge, process, observed_inputs = desktop_input_runtime

    # The first foreground frame publishes P2 as the native desktop capture
    # owner.  A background event is then drained but deliberately discarded.
    runtime.input(True, native_pause_open=False)
    assert runtime.native_desktop_capture_owner == "p2"
    sources, edge = _publish_command_alias_sample(
        process, bridge, 0, 1, True
    )
    assert edge
    runtime.input(False, native_pause_open=False)
    assert runtime.native_desktop_capture_owner is None
    assert runtime.dispatch.call_args_list == []

    runtime.input(True, native_pause_open=False)
    sources, edge = _publish_command_alias_sample(
        process, bridge, sources, 1, False
    )
    assert not edge
    sources, edge = _publish_command_alias_sample(
        process, bridge, sources, 1, True
    )
    assert edge
    sources, duplicate_edge = _publish_command_alias_sample(
        process, bridge, sources, 2, True
    )
    assert not duplicate_edge

    runtime.input(True, native_pause_open=False)
    assert runtime.dispatch.call_args_list == [mock.call(InputCommand.TOGGLE)]
    assert runtime.model.mode is MenuMode.LIST

    sources, held_edge = _publish_command_alias_sample(
        process, bridge, sources, 2, True
    )
    assert not held_edge
    runtime.input(True, native_pause_open=False)
    assert runtime.dispatch.call_args_list == [mock.call(InputCommand.TOGGLE)]

    sources, _ = _publish_command_alias_sample(process, bridge, sources, 1, False)
    sources, _ = _publish_command_alias_sample(process, bridge, sources, 2, False)
    sources, edge = _publish_command_alias_sample(
        process, bridge, sources, 2, True
    )
    assert edge
    runtime.input(True, native_pause_open=False)

    assert runtime.dispatch.call_args_list == [
        mock.call(InputCommand.TOGGLE),
        mock.call(InputCommand.CONFIRM),
    ]
    assert [controls.confirm for controls in observed_inputs] == [
        False,
        False,
        False,
        True,
        False,
        True,
    ]


def test_direct_m1_is_excluded_while_native_bridge_owns_desktop_command(
    desktop_input_runtime,
):
    runtime, _bridge, _process, _observed_inputs = desktop_input_runtime

    # Establish released edge history, then press only M1.  The native raw
    # bridge is the sole M1 producer while co-op owns COMMAND, so the ordinary
    # keyboard poller must not manufacture a duplicate confirm.
    runtime.input(True, native_pause_open=False)
    runtime.dispatch.reset_mock()

    with mock.patch.object(
        live.control_bindings._user32,
        "GetAsyncKeyState",
        side_effect=lambda vk: 0x8000 if int(vk) == 0x01 else 0,
    ):
        runtime.input(True, native_pause_open=False)

    runtime.dispatch.assert_not_called()

    # Excluding M1 is narrow: a separately configured keyboard Confirm still
    # reaches the desktop owner.
    runtime.input(True, native_pause_open=False)
    with mock.patch.object(
        live.control_bindings._user32,
        "GetAsyncKeyState",
        side_effect=lambda vk: 0x8000 if int(vk) == 0x20 else 0,
    ):
        runtime.input(True, native_pause_open=False)

    runtime.dispatch.assert_called_once_with(InputCommand.TOGGLE)


def test_p2_desktop_owner_suppresses_only_mouse_look_and_coop_off_restores_it(
    desktop_input_runtime,
):
    runtime, bridge, _process, _observed_inputs = desktop_input_runtime

    runtime.input(True, native_pause_open=False)
    assert bridge.policy.flags & live.NATIVE_INPUT_POLICY_SUPPRESS_MOUSE_LOOK
    assert bridge.policy.flags & live.NATIVE_INPUT_POLICY_CAPTURE_DESKTOP

    runtime.coop_runtime_active = False
    runtime._route_coop_disabled_input = mock.Mock()
    runtime.input(True, native_pause_open=False)

    assert not (bridge.policy.flags & live.NATIVE_INPUT_POLICY_SUPPRESS_MOUSE_LOOK)
    assert not (bridge.policy.flags & live.NATIVE_INPUT_POLICY_CAPTURE_DESKTOP)
    runtime._route_coop_disabled_input.assert_called_once()


def test_closing_follower_selector_keeps_native_command_firewall(
    desktop_input_runtime,
):
    runtime, bridge, _process, _observed_inputs = desktop_input_runtime
    runtime.session_snapshot.bridge_ownership_allowed = True
    runtime.transition_kind = "closing"

    runtime.input(True, native_pause_open=False)

    assert bridge.policy.other_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert bridge.policy.flags & live.NATIVE_INPUT_POLICY_CAPTURE_DESKTOP


def test_native_frontend_close_settle_rearms_replacement_firewalls(
    desktop_input_runtime,
):
    runtime, bridge, _process, _observed_inputs = desktop_input_runtime
    runtime.p1_visual_replacement = True
    runtime.session_snapshot.bridge_ownership_allowed = True
    runtime.native_frontend_custom_suspended = True
    runtime.native_frontend_observed_open = True

    # The Party frontend has already closed, but its visual/state suspension is
    # intentionally retained for a short settle interval.  That local latch is
    # not authority to leave gameplay COMMAND/directions native during the gap.
    runtime.session_snapshot.native_menu_open = False
    runtime.session_snapshot.native_modal_active = False
    runtime._route_native_frontend_input.return_value = True
    runtime.input(True, native_pause_open=False)

    assert bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert bridge.policy.other_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert (
        bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_DIRECTION_MASK
        == live.NATIVE_INPUT_DIRECTION_MASK
    )
    assert (
        bridge.policy.other_semantic_mask & live.NATIVE_INPUT_DIRECTION_MASK
        == live.NATIVE_INPUT_DIRECTION_MASK
    )
    runtime._route_native_frontend_input.assert_called_once()
    runtime._handle_p1_battle_input.assert_not_called()


def test_forbidden_native_frontend_instance_never_ages_into_external_trust(
    desktop_input_runtime,
):
    runtime, bridge, _process, _observed_inputs = desktop_input_runtime
    snapshot = runtime.session_snapshot
    runtime.p1_visual_replacement = True
    runtime.p1_transition_kind = "closing"
    snapshot.bridge_ownership_allowed = True
    snapshot.native_menu_open = True
    bridge.pulse = mock.Mock()

    # The raw native bit appears while the custom leader selector is closing.
    # It is rejected, cancelled once, and recorded as a forbidden provenance
    # instance rather than as a candidate Map/external frontend.
    runtime.maintain_native_frontend_suspension(snapshot, now=10.0)
    with mock.patch.object(live.time, "monotonic", return_value=10.0):
        runtime.input(True, native_pause_open=False)
    bridge.pulse.assert_called_once_with(live.NATIVE_DIALOG_CANCEL)
    assert runtime.unexpected_native_menu_cancel_latched
    assert runtime.native_frontend_open_candidate_since is None

    # Even after every lifecycle guard has expired and far more than the open
    # settle interval has elapsed, the same continuously-open bit cannot be
    # laundered into trusted external provenance.
    runtime.p1_transition_kind = None
    for now in (10.10, 10.50, 11.0, 30.0):
        runtime.maintain_native_frontend_suspension(snapshot, now=now)
        with mock.patch.object(live.time, "monotonic", return_value=now):
            runtime.input(True, native_pause_open=False)
        assert not runtime.native_frontend_input_trusted(snapshot)
        assert not runtime.native_frontend_custom_suspended
        assert not runtime.native_frontend_observed_open
        assert runtime.native_frontend_open_candidate_since is None
        for semantic_mask in (
            bridge.policy.p1_semantic_mask,
            bridge.policy.other_semantic_mask,
        ):
            assert semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
            assert (
                semantic_mask & live.NATIVE_INPUT_LEADER_MASK
                == live.NATIVE_INPUT_LEADER_MASK
            )
    bridge.pulse.assert_called_once_with(live.NATIVE_DIALOG_CANCEL)


def test_stable_external_frontend_requires_clear_edge_and_settle_before_passthrough(
    desktop_input_runtime,
):
    runtime, bridge, _process, _observed_inputs = desktop_input_runtime
    snapshot = runtime.session_snapshot
    runtime.p1_visual_replacement = True
    snapshot.bridge_ownership_allowed = True
    bridge.pulse = mock.Mock()

    # Clear is the only event that ends a previously rejected provenance
    # transaction.  A new clean open outside lifecycle guards may represent an
    # allowed stock frontend such as Map, but its first raw frame is failclosed.
    runtime.unexpected_native_menu_cancel_latched = True
    snapshot.native_menu_open = False
    runtime.maintain_native_frontend_suspension(snapshot, now=40.0)
    assert not runtime.unexpected_native_menu_cancel_latched

    snapshot.native_menu_open = True
    runtime.maintain_native_frontend_suspension(snapshot, now=40.10)
    with mock.patch.object(live.time, "monotonic", return_value=40.10):
        runtime.input(True, native_pause_open=False)
    assert runtime.native_frontend_open_candidate_since == 40.10
    assert not runtime.native_frontend_input_trusted(snapshot)
    assert bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert bridge.policy.other_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_LEADER_MASK
    assert bridge.policy.other_semantic_mask & live.NATIVE_INPUT_LEADER_MASK
    bridge.pulse.assert_not_called()

    def adopt_external(source, *, now):
        runtime.native_frontend_custom_suspended = True
        runtime.native_frontend_suspended_since = now
        runtime.native_frontend_suspension_source = source
        runtime.native_frontend_open_candidate_since = None
        runtime.native_frontend_close_candidate_since = 0.0

    runtime.suspend_custom_menus_for_native_frontend.side_effect = adopt_external
    adopted_at = 40.10 + live.NATIVE_FRONTEND_OPEN_SETTLE_SECONDS + 0.01
    runtime.maintain_native_frontend_suspension(snapshot, now=adopted_at)
    assert runtime.native_frontend_input_trusted(snapshot)
    assert runtime.native_frontend_suspension_source == "external"
    assert runtime.native_frontend_observed_open

    # Non-P1 COMMAND and every Leader semantic release only after provenance
    # is trusted.  P1 COMMAND deliberately retains its own short native-menu
    # settle guard, then also releases.
    with mock.patch.object(live.time, "monotonic", return_value=adopted_at):
        runtime.input(True, native_pause_open=False)
    assert bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert not bridge.policy.other_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert not bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_LEADER_MASK
    assert not bridge.policy.other_semantic_mask & live.NATIVE_INPUT_LEADER_MASK

    p1_settled_at = adopted_at + live.NATIVE_MENU_SETTLE_SECONDS + 0.01
    with mock.patch.object(live.time, "monotonic", return_value=p1_settled_at):
        runtime.input(True, native_pause_open=False)
    assert not bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert not bridge.policy.other_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK


@pytest.mark.parametrize(
    "owner_availability,bridge_ready",
    ((None, False), (True, True)),
    ids=("unreadable-rebuild", "stone-classified-unavailable-upstream"),
)
def test_ucoff_without_a_dead_leader_latch_keeps_command_and_leader_firewalls(
    desktop_input_runtime,
    owner_availability,
    bridge_ready,
):
    runtime, bridge, _process, _observed_inputs = desktop_input_runtime
    runtime.p1_visual_replacement = True
    runtime.custom_ui_suppressed = True
    runtime.session_snapshot.user_control_enabled = False
    runtime.session_snapshot.bridge_ownership_allowed = bridge_ready

    def availability(_process, actor):
        if not actor:
            return None
        return owner_availability if actor == 0x1000 else True

    with mock.patch.object(live, "leader_control_availability", side_effect=availability):
        runtime.input(True, native_pause_open=False)

    assert not runtime.dead_leader_menu_gate.pending
    assert bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert bridge.policy.other_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    for semantic_mask in (
        bridge.policy.p1_semantic_mask,
        bridge.policy.other_semantic_mask,
    ):
        assert semantic_mask & live.NATIVE_INPUT_DIRECTION_MASK == (
            live.NATIVE_INPUT_DIRECTION_MASK
        )
        # Pause remains an explicit native exception and is never swallowed by
        # the replacement firewall itself.
        assert not semantic_mask & live.native_semantic_mask(live.NATIVE_DIALOG_PAUSE)
    assert not bridge.policy.flags & live.NATIVE_INPUT_POLICY_CAPTURE_DESKTOP


def test_cancel_close_and_held_navigation_keep_lb_rb_and_dpad_native_blocked(
    desktop_input_runtime,
):
    runtime, bridge, _process, _observed_inputs = desktop_input_runtime
    runtime.p1_visual_replacement = True
    runtime.session_snapshot.bridge_ownership_allowed = True
    runtime.p1_transition_kind = "closing"
    runtime.open_p1_leader_selector = mock.Mock()
    bridge.pulse = mock.Mock()

    runtime.input(True, native_pause_open=False)
    expected = live.NATIVE_INPUT_CUSTOM_DIRECTION_MASK
    assert bridge.policy.p1_semantic_mask & expected == expected
    assert bridge.policy.other_semantic_mask & expected == expected

    # The visual close completed, but the same physical shoulders/d-pad remain
    # held on the following frame.  The release guard owns all six navigation
    # semantics until release instead of leaking LB into the stock leader menu.
    runtime.p1_transition_kind = None
    runtime.dispatch.reset_mock()
    with mock.patch.object(
        live,
        "custom_directional_control_down",
        return_value=True,
    ):
        runtime.input(True, native_pause_open=False)

    for semantic_mask in (
        bridge.policy.p1_semantic_mask,
        bridge.policy.other_semantic_mask,
    ):
        assert semantic_mask & expected == expected
        assert semantic_mask & live.NATIVE_INPUT_LB_MASK
        assert semantic_mask & live.native_semantic_mask(live.NATIVE_DIALOG_RB)
        assert semantic_mask & live.native_semantic_mask(live.NATIVE_DIALOG_LEFT)
        assert semantic_mask & live.native_semantic_mask(live.NATIVE_DIALOG_RIGHT)
    runtime.open_p1_leader_selector.assert_not_called()
    bridge.pulse.assert_not_called()
    runtime.dispatch.assert_not_called()


def test_first_follower_direction_opens_selector_without_advancing_anchor():
    interactions = live.WorldInteractions(None, None, None, False, False, False)

    p2 = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    p2.model = SimpleNamespace(mode=MenuMode.CLOSED)
    p2.native_interaction_router = SimpleNamespace(active=False)
    p2.open_p2_character_selector = mock.Mock(
        side_effect=lambda: setattr(p2.model, "mode", MenuMode.TARGET)
    )
    p2.dispatch = mock.Mock()
    p2._handle_p2_battle_input(
        input_role_generation=1,
        now=1.0,
        actor=0x2000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        interaction_consumed=False,
        controls=live.PlayerMenuInput(
            character_up=True,
            character_direction_ready=True,
        ),
        interactions=interactions,
    )
    p2.open_p2_character_selector.assert_called_once_with()
    p2.dispatch.assert_not_called()

    p3 = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    p3.p3_model = SimpleNamespace(mode=MenuMode.CLOSED)
    p3.open_p3_character_selector = mock.Mock(
        side_effect=lambda: setattr(p3.p3_model, "mode", MenuMode.TARGET)
    )
    p3.dispatch_p3 = mock.Mock()
    consumed = p3._handle_p3_battle_input(
        input_role_generation=1,
        now=1.0,
        actor=0x3000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        controls=live.PlayerMenuInput(
            character_down=True,
            character_direction_ready=True,
        ),
        interactions=interactions,
    )
    assert consumed
    p3.open_p3_character_selector.assert_called_once_with()
    p3.dispatch_p3.assert_not_called()


def test_dead_follower_direction_cannot_open_character_selector():
    interactions = live.WorldInteractions(None, None, None, False, False, False)

    p2 = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    p2.model = SimpleNamespace(mode=MenuMode.CLOSED)
    p2.native_interaction_router = SimpleNamespace(active=False)
    p2.open_p2_character_selector = mock.Mock()
    p2.dispatch = mock.Mock()
    p2._handle_p2_battle_input(
        input_role_generation=1,
        now=1.0,
        actor=0x2000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=False,
        confirm_reopen_ready=True,
        interaction_consumed=False,
        controls=live.PlayerMenuInput(
            character_up=True,
            character_direction_ready=True,
        ),
        interactions=interactions,
    )
    p2.open_p2_character_selector.assert_not_called()

    p3 = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    p3.p3_model = SimpleNamespace(mode=MenuMode.CLOSED)
    p3.native_interaction_router = SimpleNamespace(active=False)
    p3.open_p3_character_selector = mock.Mock()
    p3.dispatch_p3 = mock.Mock()
    consumed = p3._handle_p3_battle_input(
        input_role_generation=1,
        now=1.0,
        actor=0x3000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=False,
        confirm_reopen_ready=True,
        controls=live.PlayerMenuInput(
            character_down=True,
            character_direction_ready=True,
        ),
        interactions=interactions,
    )
    assert not consumed
    p3.open_p3_character_selector.assert_not_called()


def test_dead_p3_cannot_publish_selector_even_if_an_open_call_slips_through():
    process = MemoryProcess()
    actor = 0x730000
    battle_keep = 0x740000
    handle = 0x200028
    process.seed(actor + 0x08, struct.pack("<I", handle))
    process.seed(actor + 0x698, struct.pack("<Q", battle_keep))
    process.seed(battle_keep + 0x48, struct.pack("<I", 0))

    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.process = process
    runtime.session_snapshot = SimpleNamespace(
        identity=live.session_state.SessionIdentity(1, 0, 4)
    )
    runtime.party = [
        ("Ashe", 0x100027, 0x710000),
        ("Vaan", 0x100026, 0x720000),
        ("Basch", handle, actor),
    ]
    runtime.active_party = [
        (1, "Ashe", 0x100027, 0x710000),
        (0, "Vaan", 0x100026, 0x720000),
        (4, "Basch", handle, actor),
    ]
    runtime.p3_model = SimpleNamespace(
        mode=MenuMode.CLOSED,
        issuer=handle,
        issuer_party_member_id=4,
    )
    runtime.p3_character_direction_guard = mock.Mock()
    runtime.refresh_actors = mock.Mock()

    runtime.open_p3_character_selector()

    assert runtime.p3_model.mode is MenuMode.CLOSED
    runtime.p3_character_direction_guard.arm.assert_called_once()


@pytest.mark.parametrize(
    "status_mask",
    (
        1 << 1,   # Stone
        1 << 3,   # Stop
        1 << 4,   # Sleep
        1 << 5,   # Confuse
        1 << 13,  # Disable
        1 << 31,  # X-Zone
    ),
)
def test_command_blocking_status_cannot_publish_p3_selector(status_mask):
    process = MemoryProcess()
    actor = 0x750000
    battle_keep = 0x760000
    handle = 0x200028
    process.seed(actor + 0x08, struct.pack("<I", handle))
    process.seed(actor + 0x698, struct.pack("<Q", battle_keep))
    process.seed(battle_keep + 0x48, struct.pack("<I", 100))
    process.seed(
        battle_keep + live.TEMPORARY_STATUS_EFFECTS_OFFSET,
        struct.pack("<I", status_mask),
    )

    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.process = process
    runtime.session_snapshot = SimpleNamespace(
        identity=live.session_state.SessionIdentity(1, 0, 4)
    )
    runtime.party = [
        ("Ashe", 0x100027, 0x710000),
        ("Vaan", 0x100026, 0x720000),
        ("Basch", handle, actor),
    ]
    runtime.active_party = [
        (1, "Ashe", 0x100027, 0x710000),
        (0, "Vaan", 0x100026, 0x720000),
        (4, "Basch", handle, actor),
    ]
    runtime.p3_model = SimpleNamespace(
        mode=MenuMode.CLOSED,
        issuer=handle,
        issuer_party_member_id=4,
    )
    runtime.p3_character_direction_guard = mock.Mock()
    runtime.refresh_actors = mock.Mock()

    runtime.open_p3_character_selector()

    assert runtime.p3_model.mode is MenuMode.CLOSED
    runtime.p3_character_direction_guard.arm.assert_called_once()


def test_legacy_back_change_character_hotkey_cannot_open_follower_selectors():
    interactions = live.WorldInteractions(None, None, None, False, False, False)
    p2 = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    p2.model = SimpleNamespace(mode=MenuMode.CLOSED)
    p2.native_interaction_router = SimpleNamespace(active=False)
    p2.open_p2_character_selector = mock.Mock()
    p2.dispatch = mock.Mock()
    p2._handle_p2_battle_input(
        input_role_generation=1,
        now=1.0,
        actor=0x2000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        interaction_consumed=False,
        controls=live.PlayerMenuInput(character_hotkey=True),
        interactions=interactions,
    )
    p2.open_p2_character_selector.assert_not_called()
    p2.dispatch.assert_not_called()

    p3 = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    p3.p3_model = SimpleNamespace(mode=MenuMode.CLOSED)
    p3.native_interaction_router = SimpleNamespace(active=False)
    p3.open_p3_character_selector = mock.Mock()
    p3.dispatch_p3 = mock.Mock()
    consumed = p3._handle_p3_battle_input(
        input_role_generation=1,
        now=1.0,
        actor=0x3000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        controls=live.PlayerMenuInput(character_hotkey=True),
        interactions=interactions,
    )
    assert not consumed
    p3.open_p3_character_selector.assert_not_called()
    p3.dispatch_p3.assert_not_called()


def test_first_p1_direction_opens_leader_selector_without_advancing_anchor(
    desktop_input_runtime,
):
    runtime, _bridge, _process, _observed_inputs = desktop_input_runtime
    live.control_bindings.set_desktop_owner(runtime.bindings.config, "p1")
    runtime.bindings._reconcile_active_devices()
    runtime.p1_visual_replacement = True
    runtime.session_snapshot.bridge_ownership_allowed = True
    runtime.dead_leader_menu_gate.update = mock.Mock(return_value=False)
    runtime.dispatch_p1 = mock.Mock()
    runtime.open_p1_leader_selector = mock.Mock(
        side_effect=lambda: setattr(runtime.p1_model, "mode", MenuMode.TARGET)
    )

    with (
        mock.patch.object(
            runtime.bindings,
            "repeat",
            side_effect=lambda player, action: (player, action)
            == ("p1", "menu_up"),
        ),
        mock.patch.object(
            runtime.bindings,
            "edge",
            side_effect=lambda player, action: (player, action)
            == ("p1", "menu_up"),
        ),
    ):
        runtime.input(True, native_pause_open=False)

    runtime.open_p1_leader_selector.assert_called_once_with()
    runtime.dispatch_p1.assert_not_called()


def test_p1_direction_held_through_pause_requires_release_before_opening(
    desktop_input_runtime,
):
    runtime, _bridge, _process, _observed_inputs = desktop_input_runtime
    live.control_bindings.set_desktop_owner(runtime.bindings.config, "p1")
    runtime.bindings._reconcile_active_devices()
    runtime.p1_visual_replacement = True
    runtime.session_snapshot.bridge_ownership_allowed = True
    runtime.dead_leader_menu_gate.update = mock.Mock(return_value=False)
    runtime.dispatch_p1 = mock.Mock()
    runtime.open_p1_leader_selector = mock.Mock(
        side_effect=lambda: setattr(runtime.p1_model, "mode", MenuMode.TARGET)
    )
    held = {"down": True, "previous": False}

    def direction_edge(player, action):
        if (player, action) != ("p1", "menu_up"):
            return False
        edge = bool(held["down"] and not held["previous"])
        held["previous"] = bool(held["down"])
        return edge

    def direction_repeat(player, action):
        return bool((player, action) == ("p1", "menu_up") and held["down"])

    with (
        mock.patch.object(runtime.bindings, "edge", side_effect=direction_edge),
        mock.patch.object(runtime.bindings, "repeat", side_effect=direction_repeat),
    ):
        runtime.input(True, native_pause_open=True)
        runtime.input(True, native_pause_open=False)
        runtime.open_p1_leader_selector.assert_not_called()

        held["down"] = False
        runtime.input(True, native_pause_open=False)
        held["down"] = True
        runtime.input(True, native_pause_open=False)

    runtime.open_p1_leader_selector.assert_called_once_with()


@pytest.mark.parametrize("horizontal_action", ("menu_left", "menu_right"))
def test_p1_horizontal_aliases_never_open_the_leader_selector(
    desktop_input_runtime,
    horizontal_action,
):
    runtime, _bridge, _process, _observed_inputs = desktop_input_runtime
    live.control_bindings.set_desktop_owner(runtime.bindings.config, "p1")
    runtime.bindings._reconcile_active_devices()
    runtime.p1_visual_replacement = True
    runtime.session_snapshot.bridge_ownership_allowed = True
    runtime.dead_leader_menu_gate.update = mock.Mock(return_value=False)
    runtime.open_p1_leader_selector = mock.Mock()

    with (
        mock.patch.object(
            runtime.bindings,
            "repeat",
            side_effect=lambda player, action: (player, action)
            == ("p1", horizontal_action),
        ),
        mock.patch.object(
            runtime.bindings,
            "edge",
            side_effect=lambda player, action: (player, action)
            == ("p1", horizontal_action),
        ),
    ):
        runtime.input(True, native_pause_open=False)

    runtime.open_p1_leader_selector.assert_not_called()


@pytest.mark.parametrize(
    "direction_controls",
    (
        live.PlayerMenuInput(up=True),
        live.PlayerMenuInput(down=True),
        # P1 LB/RB are aliases of the horizontal menu actions.
        live.PlayerMenuInput(left=True, page_up=True),
        live.PlayerMenuInput(right=True, page_down=True),
    ),
)
def test_passthrough_direction_can_never_arm_a_later_native_leader_confirm(
    direction_controls,
):
    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.p1_model = SimpleNamespace(mode=MenuMode.CLOSED)
    runtime.p1_menu_reopen_allowed_at = 0.0
    runtime.native_dialog_bridge = SimpleNamespace(pulse=mock.Mock())
    runtime.dispatch_p1 = mock.Mock()
    runtime.native_interaction_router = SimpleNamespace(trigger_native_p1=mock.Mock())
    runtime._release_native_wait_presentation_for_treasure = mock.Mock()
    interactions = live.WorldInteractions(None, None, None, False, False, False)

    # Direction/shoulder sampled while a native exception owns input is inert;
    # returning to field and pressing Confirm may only open the custom command
    # tree.  The removed ten-second native-Leader arm has no residual state.
    runtime._handle_p1_battle_input(
        now=1.0,
        actor=0x1000,
        native_open=False,
        binding_enabled=False,
        custom_menu_allowed=False,
        confirm_reopen_ready=True,
        controls=direction_controls,
        interactions=interactions,
    )
    runtime._handle_p1_battle_input(
        now=2.0,
        actor=0x1000,
        native_open=False,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        controls=live.PlayerMenuInput(confirm=True),
        interactions=interactions,
    )

    runtime.dispatch_p1.assert_called_once_with(InputCommand.TOGGLE)
    runtime.native_dialog_bridge.pulse.assert_not_called()


def test_p1_leader_selector_cancel_uses_its_transactional_close_path() -> None:
    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.p1_model = SimpleNamespace(mode=MenuMode.TARGET)
    runtime.p1_leader_selector_active = mock.Mock(return_value=True)
    runtime.close_p1_leader_selector = mock.Mock()
    runtime.dispatch_p1 = mock.Mock()

    runtime._handle_p1_battle_input(
        now=1.0,
        actor=0x1000,
        native_open=False,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        controls=live.PlayerMenuInput(cancel=True, up=True, left=True),
        interactions=live.WorldInteractions(None, None, None, False, False, False),
    )

    runtime.close_p1_leader_selector.assert_called_once_with()
    runtime.dispatch_p1.assert_not_called()


def test_p1_leader_selector_accepts_only_one_vertical_action_per_frame() -> None:
    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.p1_model = SimpleNamespace(mode=MenuMode.TARGET)
    runtime.p1_leader_selector_active = mock.Mock(return_value=True)
    runtime.close_p1_leader_selector = mock.Mock()
    runtime.dispatch_p1 = mock.Mock()
    interactions = live.WorldInteractions(None, None, None, False, False, False)

    runtime._handle_p1_battle_input(
        now=1.0,
        actor=0x1000,
        native_open=False,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        controls=live.PlayerMenuInput(left=True, right=True, target_group=True),
        interactions=interactions,
    )
    runtime.dispatch_p1.assert_not_called()

    runtime._handle_p1_battle_input(
        now=1.1,
        actor=0x1000,
        native_open=False,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        controls=live.PlayerMenuInput(up=True),
        interactions=interactions,
    )
    runtime.dispatch_p1.assert_called_once_with(InputCommand.UP)


def test_follower_character_selectors_reject_every_horizontal_alias() -> None:
    interactions = live.WorldInteractions(None, None, None, False, False, False)
    horizontal = live.PlayerMenuInput(
        left=True,
        right=True,
        page_up=True,
        page_down=True,
        target_group=True,
        target_group_direction=1,
    )

    p2 = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    p2.model = SimpleNamespace(mode=MenuMode.TARGET)
    p2.p2_character_selector_active = mock.Mock(return_value=True)
    p2.close_p2_character_selector = mock.Mock()
    p2.dispatch = mock.Mock()
    p2._handle_p2_battle_input(
        input_role_generation=1,
        now=1.0,
        actor=0x2000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        interaction_consumed=False,
        controls=horizontal,
        interactions=interactions,
    )
    p2.dispatch.assert_not_called()
    p2._handle_p2_battle_input(
        input_role_generation=1,
        now=1.1,
        actor=0x2000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        interaction_consumed=False,
        controls=live.PlayerMenuInput(up=True),
        interactions=interactions,
    )
    p2.dispatch.assert_called_once_with(InputCommand.UP)

    p3 = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    p3.p3_model = SimpleNamespace(mode=MenuMode.TARGET)
    p3.p3_character_selector_active = mock.Mock(return_value=True)
    p3.close_p3_character_selector = mock.Mock()
    p3.dispatch_p3 = mock.Mock()
    p3.role_generation = 1
    consumed = p3._handle_p3_battle_input(
        input_role_generation=1,
        now=1.0,
        actor=0x3000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        controls=horizontal,
        interactions=interactions,
    )
    assert not consumed
    p3.dispatch_p3.assert_not_called()
    consumed = p3._handle_p3_battle_input(
        input_role_generation=1,
        now=1.1,
        actor=0x3000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        controls=live.PlayerMenuInput(down=True),
        interactions=interactions,
    )
    assert not consumed
    p3.dispatch_p3.assert_called_once_with(InputCommand.DOWN)


def test_follower_character_selector_cancel_is_transactional_and_exclusive() -> None:
    interactions = live.WorldInteractions(None, None, None, False, False, False)
    cancel_with_noise = live.PlayerMenuInput(
        cancel=True,
        confirm=True,
        up=True,
        page_up=True,
        target_group=True,
    )

    p2 = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    p2.model = SimpleNamespace(mode=MenuMode.TARGET)
    p2.p2_character_selector_active = mock.Mock(return_value=True)
    p2.close_p2_character_selector = mock.Mock()
    p2.dispatch = mock.Mock()
    p2._handle_p2_battle_input(
        input_role_generation=1,
        now=1.0,
        actor=0x2000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        interaction_consumed=False,
        controls=cancel_with_noise,
        interactions=interactions,
    )
    p2.close_p2_character_selector.assert_called_once_with()
    p2.dispatch.assert_not_called()

    p3 = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    p3.p3_model = SimpleNamespace(mode=MenuMode.TARGET)
    p3.p3_character_selector_active = mock.Mock(return_value=True)
    p3.close_p3_character_selector = mock.Mock()
    p3.dispatch_p3 = mock.Mock()
    p3.role_generation = 1
    consumed = p3._handle_p3_battle_input(
        input_role_generation=1,
        now=1.0,
        actor=0x3000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        controls=cancel_with_noise,
        interactions=interactions,
    )
    assert consumed
    p3.close_p3_character_selector.assert_called_once_with()
    p3.dispatch_p3.assert_not_called()


def test_follower_f_opens_battle_menu_but_never_confirms_an_open_item(
    desktop_input_runtime,
):
    runtime, _bridge, _process, observed_inputs = desktop_input_runtime

    # Establish released history first; a helper that starts while F is held
    # intentionally absorbs that inherited hold.
    runtime.input(True, native_pause_open=False)
    runtime.dispatch.reset_mock()

    def f_down(vk):
        return 0x8000 if int(vk) == ord("F") else 0

    with mock.patch.object(
        live.control_bindings._user32,
        "GetAsyncKeyState",
        side_effect=f_down,
    ):
        runtime.input(True, native_pause_open=False)
        # Holding F cannot manufacture a second edge.
        runtime.input(True, native_pause_open=False)

    assert runtime.dispatch.call_args_list == [mock.call(InputCommand.TOGGLE)]
    assert runtime.model.mode is MenuMode.LIST
    assert observed_inputs[-1].confirm is False

    # Release, press F again with the tree already open, and prove Square's
    # battle-menu semantic is not converted into Confirm.
    runtime.input(True, native_pause_open=False)
    with mock.patch.object(
        live.control_bindings._user32,
        "GetAsyncKeyState",
        side_effect=f_down,
    ):
        runtime.input(True, native_pause_open=False)

    assert runtime.dispatch.call_args_list == [mock.call(InputCommand.TOGGLE)]
    assert all(call != mock.call(InputCommand.CONFIRM) for call in runtime.dispatch.mock_calls)


def test_p3_battle_menu_edge_opens_only_p3_and_is_inert_once_open():
    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.p3_model = SimpleNamespace(mode=MenuMode.CLOSED)
    runtime.p3_menu_reopen_allowed_at = 0.0
    runtime.native_interaction_router = SimpleNamespace(
        active=False,
        trigger=mock.Mock(),
        trigger_atomic_follower_npc=mock.Mock(),
    )
    runtime.p3_character_selector_active = mock.Mock(return_value=False)
    runtime.dispatch_p3 = mock.Mock(
        side_effect=lambda command: (
            setattr(runtime.p3_model, "mode", MenuMode.LIST)
            if command is InputCommand.TOGGLE
            else None
        )
    )
    runtime.dispatch = mock.Mock()
    interactions = live.WorldInteractions(
        p1=None,
        p2=None,
        p3=(2, 0x3000, 0, 0),
        p1_ready=False,
        p2_present=False,
        p3_present=True,
    )

    opened = runtime._handle_p3_battle_input(
        input_role_generation=1,
        now=1.0,
        actor=0x3000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        controls=live.PlayerMenuInput(),
        interactions=interactions,
        battle_menu_edge=True,
    )

    assert opened is True
    runtime.dispatch_p3.assert_called_once_with(InputCommand.TOGGLE)
    runtime.dispatch.assert_not_called()
    runtime.native_interaction_router.trigger.assert_not_called()
    runtime.native_interaction_router.trigger_atomic_follower_npc.assert_not_called()

    runtime.dispatch_p3.reset_mock()
    opened = runtime._handle_p3_battle_input(
        input_role_generation=1,
        now=2.0,
        actor=0x3000,
        leader_actor=0x1000,
        binding_enabled=True,
        custom_menu_allowed=True,
        confirm_reopen_ready=True,
        controls=live.PlayerMenuInput(),
        interactions=interactions,
        battle_menu_edge=True,
    )

    assert opened is False
    runtime.dispatch_p3.assert_not_called()


def test_follower_escape_uses_one_native_pause_pulse_for_open_and_close(
    desktop_input_runtime,
):
    runtime, bridge, _process, _observed_inputs = desktop_input_runtime
    runtime.session_snapshot.loading = False
    runtime.session_snapshot.cutscene = False

    runtime.input(True, native_pause_open=False)

    def escape_down(vk):
        return 0x8000 if int(vk) == 0x1B else 0

    with (
        mock.patch.object(
            live.control_bindings._user32,
            "GetAsyncKeyState",
            side_effect=escape_down,
        ),
        mock.patch.object(bridge, "pulse", wraps=bridge.pulse) as pulse,
    ):
        runtime.input(True, native_pause_open=False)
        runtime.input(True, native_pause_open=False)
        pulse.assert_called_once_with(live.NATIVE_DIALOG_PAUSE)

    # Release before testing the close edge.  P1's physical Start remains on
    # the stock path; only the filtered follower Esc is explicitly returned.
    runtime.input(True, native_pause_open=True)
    with (
        mock.patch.object(
            live.control_bindings._user32,
            "GetAsyncKeyState",
            side_effect=escape_down,
        ),
        mock.patch.object(bridge, "pulse", wraps=bridge.pulse) as pulse,
    ):
        runtime.input(True, native_pause_open=True)
        pulse.assert_called_once_with(live.NATIVE_DIALOG_PAUSE)


@pytest.mark.parametrize("player", ("p2", "p3"))
def test_each_follower_pause_edge_generates_exactly_one_native_toggle(
    desktop_input_runtime,
    player,
):
    runtime, bridge, _process, _observed_inputs = desktop_input_runtime
    bridge.pulse = mock.Mock()

    def edge(candidate, action):
        return candidate == player and action == "native_pause"

    with mock.patch.object(runtime.bindings, "edge", side_effect=edge):
        runtime.input(True, native_pause_open=False)

    bridge.pulse.assert_called_once_with(live.NATIVE_DIALOG_PAUSE)


def test_p1_physical_pause_never_receives_a_synthetic_duplicate(
    desktop_input_runtime,
):
    runtime, bridge, _process, _observed_inputs = desktop_input_runtime
    bridge.pulse = mock.Mock()

    def edge(candidate, action):
        return candidate == "p1" and action == "native_pause"

    with mock.patch.object(runtime.bindings, "edge", side_effect=edge):
        runtime.input(True, native_pause_open=False)

    bridge.pulse.assert_not_called()


@pytest.mark.parametrize("follower", ("p2", "p3"))
def test_open_pause_simultaneous_p1_and_follower_edges_never_reopen_by_pulse(
    desktop_input_runtime,
    follower,
):
    runtime, bridge, _process, _observed_inputs = desktop_input_runtime
    bridge.pulse = mock.Mock()

    def edge(candidate, action):
        return action == "native_pause" and candidate in ("p1", follower)

    with mock.patch.object(runtime.bindings, "edge", side_effect=edge):
        runtime.input(True, native_pause_open=True)

    # P1 Start reaches FF12 physically and closes the proven Pause screen.  A
    # second synthetic follower edge in the same sampled frame would reopen it.
    bridge.pulse.assert_not_called()


def test_wheel_ring_discards_the_closed_route_burst_after_opening(
    desktop_input_runtime,
):
    runtime, bridge, process, observed_inputs = desktop_input_runtime
    runtime.input(True, native_pause_open=False)

    producer = bridge.input_state + live.NATIVE_INPUT_EVENT_PRODUCER_OFFSET
    for sequence, delta in enumerate((0xFF, -0xFF, 0xFF), start=1):
        _seed_event(
            process,
            bridge,
            sequence,
            live.NATIVE_INPUT_EVENT_WHEEL,
            delta,
        )
    _seed_u32(process, producer, 3)

    runtime.input(True, native_pause_open=False)

    runtime.open_p2_character_selector.assert_called_once_with()
    assert runtime.dispatch.call_args_list == []
    assert not runtime.native_desktop_pending_events
    assert not any(controls.confirm for controls in observed_inputs)

    runtime.input(True, native_pause_open=False)
    assert runtime.dispatch.call_count == 0


def test_open_wheel_selector_accepts_one_gesture_each_half_second(
    desktop_input_runtime,
):
    runtime, bridge, process, _observed_inputs = desktop_input_runtime
    routed_sounds = []
    runtime.open_p2_character_selector = mock.Mock(
        side_effect=lambda: (
            setattr(runtime.model, "mode", MenuMode.TARGET),
            routed_sounds.append("open"),
        )
    )
    runtime.dispatch = mock.Mock(side_effect=lambda _command: routed_sounds.append("step"))

    with mock.patch.object(live.time, "monotonic", return_value=9.0):
        runtime.input(True, native_pause_open=False)

    _seed_event(
        process,
        bridge,
        1,
        live.NATIVE_INPUT_EVENT_WHEEL,
        0xFF,
    )
    _seed_u32(
        process,
        bridge.input_state + live.NATIVE_INPUT_EVENT_PRODUCER_OFFSET,
        1,
    )
    with mock.patch.object(live.time, "monotonic", return_value=10.0):
        runtime.input(True, native_pause_open=False)
    runtime.open_p2_character_selector.assert_called_once_with()

    # Commit the selector's new route generation without a gesture.  This must
    # preserve the gate armed by the opening detent.
    with mock.patch.object(live.time, "monotonic", return_value=10.05):
        runtime.input(True, native_pause_open=False)
    generation = bridge.committed_policy_generation()
    for sequence, delta in ((2, 0x1FE), (3, -0xFF)):
        _seed_event(
            process,
            bridge,
            sequence,
            live.NATIVE_INPUT_EVENT_WHEEL,
            delta,
            generation=generation,
        )
    _seed_u32(
        process,
        bridge.input_state + live.NATIVE_INPUT_EVENT_PRODUCER_OFFSET,
        3,
    )
    with mock.patch.object(live.time, "monotonic", return_value=10.1):
        runtime.input(True, native_pause_open=False)
    runtime.dispatch.assert_not_called()

    _seed_event(
        process,
        bridge,
        4,
        live.NATIVE_INPUT_EVENT_WHEEL,
        -0x1FE,
        generation=bridge.committed_policy_generation(),
    )
    _seed_u32(
        process,
        bridge.input_state + live.NATIVE_INPUT_EVENT_PRODUCER_OFFSET,
        4,
    )
    with mock.patch.object(live.time, "monotonic", return_value=10.5):
        runtime.input(True, native_pause_open=False)
    runtime.dispatch.assert_called_once_with(InputCommand.DOWN)

    with mock.patch.object(live.time, "monotonic", return_value=11.0):
        runtime.input(True, native_pause_open=False)
    runtime.dispatch.assert_called_once_with(InputCommand.DOWN)
    assert routed_sounds == ["open", "step"]
    assert process.u32(
        bridge.input_state + live.NATIVE_INPUT_EVENT_CONSUMER_OFFSET
    ) == 4


def test_ring_sequence_wrap_keeps_zero_as_an_ordered_sequence():
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    producer = bridge.input_state + live.NATIVE_INPUT_EVENT_PRODUCER_OFFSET
    consumer = bridge.input_state + live.NATIVE_INPUT_EVENT_CONSUMER_OFFSET
    _seed_u32(process, producer, 1)
    _seed_u32(process, consumer, 0xFFFFFFFE)
    for sequence, delta in (
        (0xFFFFFFFF, 0xFF),
        (0, -0xFF),
        (1, 0xFF),
    ):
        _seed_event(process, bridge, sequence, live.NATIVE_INPUT_EVENT_WHEEL, delta)

    events = bridge.drain_desktop_events()

    assert [event.sequence for event in events] == [0xFFFFFFFF, 0, 1]
    assert [event.value for event in events] == [0xFF, -0xFF, 0xFF]


def test_full_wheel_ring_drops_the_signed_delta_without_replay():
    bridge = live.NativeDialogInputBridge(MemoryProcess(), 0x500000)
    wheel = bridge._build_desktop_wheel_cave()
    consume = b"\x31\xc0\x87\x81\xf0\x01\x00\x00\x89\xc2"
    restore = b"\xf0\x01\x91\xf0\x01\x00\x00"
    assert consume in wheel
    assert restore not in wheel
    assert b"\x31\xc0\x41\x87\x42\x38" in wheel


def test_wheel_event_from_a_failed_drain_cannot_replay_after_recovery(
    desktop_input_runtime,
):
    runtime, bridge, process, _observed_inputs = desktop_input_runtime
    runtime.input(True, native_pause_open=False)
    runtime.open_p2_character_selector.reset_mock()
    runtime.dispatch.reset_mock()
    generation = bridge.committed_policy_generation()
    _seed_event(
        process,
        bridge,
        1,
        live.NATIVE_INPUT_EVENT_WHEEL,
        0xFF,
        generation=generation,
    )
    _seed_u32(
        process,
        bridge.input_state + live.NATIVE_INPUT_EVENT_PRODUCER_OFFSET,
        1,
    )
    real_drain = bridge.drain_desktop_events
    attempts = 0

    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(299, "torn wheel-ring read")
        return real_drain()

    runtime._report_frame_subsystem_failure = mock.Mock()
    runtime._clear_frame_subsystem_failure = mock.Mock()
    with mock.patch.object(bridge, "drain_desktop_events", side_effect=fail_once):
        runtime.input(True, native_pause_open=False)
        assert runtime.native_desktop_route_signature is None
        runtime.input(True, native_pause_open=False)

    assert bridge.committed_policy_generation() != generation
    runtime.open_p2_character_selector.assert_not_called()
    runtime.dispatch.assert_not_called()


def test_abnormal_worker_recovery_leaves_input_firewalls_fail_closed() -> None:
    process = MemoryProcess()
    bridge = live.NativeDialogInputBridge(process, 0x500000)
    bridge.installed = True

    bridge.fail_closed_for_recovery()

    assert bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_DIRECTION_MASK
    assert bridge.policy.other_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert bridge.policy.other_semantic_mask & live.NATIVE_INPUT_DIRECTION_MASK
    assert bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_PARTY_MASK
    assert bridge.policy.other_semantic_mask & live.NATIVE_INPUT_PARTY_MASK
    assert not (
        bridge.policy.flags & live.NATIVE_INPUT_POLICY_CAPTURE_DESKTOP
    )


@pytest.mark.parametrize(
    "failed_stage",
    (
        "release",
        "policy_slot",
        "policy_commit",
        "command_sources",
        "command_seed",
        "wheel_seed",
        "event_consumer",
        "final_pulse_clear",
    ),
)
def test_fail_closed_recovery_attempts_every_independent_stage(failed_stage) -> None:
    base = 0x500000
    probe = live.NativeDialogInputBridge(MemoryProcess(), base)
    addresses = {
        "release": probe.state + 4,
        "policy_slot": (
            probe.input_state + live.NATIVE_INPUT_POLICY_SLOT_SIZE
        ),
        "policy_commit": (
            probe.input_state + live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET
        ),
        "command_sources": (
            probe.input_state + live.NATIVE_INPUT_COMMAND_SOURCES_OFFSET
        ),
        "command_seed": (
            probe.input_state + live.NATIVE_INPUT_COMMAND_SEED_OFFSET
        ),
        "wheel_seed": (
            probe.input_state + live.NATIVE_INPUT_WHEEL_SEED_OFFSET
        ),
        "event_consumer": (
            probe.input_state + live.NATIVE_INPUT_EVENT_CONSUMER_OFFSET
        ),
        "final_pulse_clear": probe.state,
    }
    process = FailOnceWriteProcess(addresses[failed_stage])
    bridge = live.NativeDialogInputBridge(process, base)
    bridge.installed = True
    _seed_u32(
        process,
        bridge.input_state + live.NATIVE_INPUT_EVENT_PRODUCER_OFFSET,
        9,
    )

    with pytest.raises(OSError):
        bridge.fail_closed_for_recovery()

    assert process.failure_injected
    assert bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert bridge.policy.p1_semantic_mask & live.NATIVE_INPUT_DIRECTION_MASK
    assert bridge.policy.other_semantic_mask & live.NATIVE_INPUT_COMMAND_MASK
    assert bridge.policy.other_semantic_mask & live.NATIVE_INPUT_DIRECTION_MASK
    assert all(address in process.write_attempts for address in addresses.values())
    assert process.u32(
        bridge.input_state + live.NATIVE_INPUT_EVENT_CONSUMER_OFFSET
    ) == 9


def test_overflow_diagnostic_is_strictly_once_per_runtime_session():
    bridge = SimpleNamespace(
        event_overflow_count=1,
        drain_desktop_events=mock.Mock(return_value=()),
    )
    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.native_dialog_bridge = bridge
    runtime.native_desktop_pending_events = deque()
    runtime.native_desktop_capture_owner = "p2"
    runtime.native_desktop_overflow_reported = 0

    with mock.patch.object(builtins, "print") as print_mock:
        runtime._collect_native_desktop_events(
            foreground=True,
            desktop_owner="p2",
        )
        bridge.event_overflow_count = 200
        runtime._collect_native_desktop_events(
            foreground=True,
            desktop_owner="p2",
        )

    print_mock.assert_called_once_with(
        "NATIVE_DESKTOP_INPUT_OVERFLOW count=1",
        flush=True,
    )


def test_native_events_require_same_foreground_desktop_owner():
    event = live.NativeDesktopInputEvent(1, live.NATIVE_INPUT_EVENT_COMMAND, 7, 9)
    bridge = SimpleNamespace(
        event_overflow_count=0,
        drain_desktop_events=mock.Mock(return_value=(event,)),
        policy_generation=9,
    )
    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.native_dialog_bridge = bridge
    runtime.native_desktop_pending_events = deque()
    runtime.native_desktop_capture_owner = "p2"
    runtime.native_desktop_overflow_reported = 0

    assert runtime._collect_native_desktop_events(
        foreground=False,
        desktop_owner="p2",
    ) == ()
    assert not runtime.native_desktop_pending_events
    assert runtime._collect_native_desktop_events(
        foreground=True,
        desktop_owner="p3",
    ) == ()
    assert not runtime.native_desktop_pending_events
    accepted = runtime._collect_native_desktop_events(
        foreground=True,
        desktop_owner="p2",
    )
    assert accepted == (event,)
    assert not runtime.native_desktop_pending_events


def test_wheel_owner_opens_the_correct_custom_selector():
    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.p1_model = SimpleNamespace(mode=MenuMode.CLOSED)
    runtime.model = SimpleNamespace(mode=MenuMode.CLOSED)
    runtime.p3_model = SimpleNamespace(mode=MenuMode.CLOSED)
    runtime.dispatch_p1 = mock.Mock()
    runtime.dispatch = mock.Mock()
    runtime.dispatch_p3 = mock.Mock()
    runtime.open_p1_leader_selector = mock.Mock(
        side_effect=lambda: setattr(runtime.p1_model, "mode", MenuMode.TARGET)
    )
    runtime.open_p2_character_selector = mock.Mock(
        side_effect=lambda: setattr(runtime.model, "mode", MenuMode.TARGET)
    )
    runtime.open_p3_character_selector = mock.Mock(
        side_effect=lambda: setattr(runtime.p3_model, "mode", MenuMode.TARGET)
    )

    assert runtime._route_desktop_wheel_step(
        "p1", -1, p1_enabled=True, p2_enabled=True, p3_enabled=True
    )
    runtime.open_p1_leader_selector.assert_called_once_with()
    runtime.dispatch_p1.assert_not_called()
    assert runtime._route_desktop_wheel_step(
        "p1", -1, p1_enabled=True, p2_enabled=True, p3_enabled=True
    )
    runtime.dispatch_p1.assert_called_once_with(InputCommand.UP)
    assert runtime._route_desktop_wheel_step(
        "p2", 1, p1_enabled=True, p2_enabled=True, p3_enabled=True
    )
    runtime.open_p2_character_selector.assert_called_once_with()
    runtime.dispatch.assert_not_called()
    assert runtime._route_desktop_wheel_step(
        "p2", 1, p1_enabled=True, p2_enabled=True, p3_enabled=True
    )
    runtime.dispatch.assert_called_once_with(InputCommand.DOWN)
    assert runtime._route_desktop_wheel_step(
        "p3", -1, p1_enabled=True, p2_enabled=True, p3_enabled=True
    )
    runtime.open_p3_character_selector.assert_called_once_with()
    runtime.dispatch_p3.assert_not_called()
    assert runtime._route_desktop_wheel_step(
        "p3", -1, p1_enabled=True, p2_enabled=True, p3_enabled=True
    )
    runtime.dispatch_p3.assert_called_once_with(InputCommand.UP)


def test_pending_p1_handoff_keeps_native_suppression_without_reopening():
    selector, suppress = live.native_leader_replacement_state(
        base_eligible=True,
        menus_closed=True,
        transitions_clear=True,
        handoff_pending=True,
    )
    assert not selector
    assert suppress
    selector, suppress = live.native_leader_replacement_state(
        base_eligible=True,
        menus_closed=True,
        transitions_clear=True,
        handoff_pending=False,
    )
    assert selector
    assert suppress


def test_p1_leader_selection_uses_the_existing_transactional_mailbox():
    actor = 0x1000
    battle_keep = 0x2000

    class LeaderProcess:
        def u64(self, address):
            return {
                actor + 0x698: battle_keep,
                battle_keep + 0x64: 0,
                actor: 0,
            }.get(address, 0)

        def u32(self, address):
            return 100 if address == battle_keep + 0x48 else 0

    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.process = LeaderProcess()
    runtime.active_party = [(1, "Ashe", 0xA11CE, actor)]
    runtime.p1_leader_handoff_sequence = 0
    runtime.p1_leader_handoff_member_id = None
    runtime.p1_leader_handoff_deadline = 0.0
    runtime.p1_leader_handoff_identity = None
    runtime.p1_leader_handoff_identity_saved = False
    runtime.p1_leader_handoff_save_retry_at = 0.0
    runtime.role_generation = 4
    runtime.last_actor_refresh = 1.0
    old_identity = live.session_state.SessionIdentity(0, 1, 2)
    new_identity = live.session_state.SessionIdentity(1, 0, 2)
    runtime.session_snapshot = SimpleNamespace(
        identity=old_identity,
        current_layout=(0, 1, 2, 0xFFFF),
        ui_layout=(0, 1, 2, 0xFFFF),
    )
    runtime.session = SimpleNamespace(path=Path("ignored.json"), reload=mock.Mock())

    with (
        mock.patch.object(
            live.legacy.coop,
            "publish_native_leader_role_transfer_active",
        ) as active_mock,
        mock.patch.object(
            live.legacy.coop,
            "request_native_leader_role_transfer",
            return_value=(77, new_identity),
        ) as request_mock,
        mock.patch.object(live.legacy.coop, "read_manual_control_mask", return_value=3),
        mock.patch.object(
            live.legacy.coop,
            "native_leader_role_transfer_outcome",
            return_value="applied",
        ) as outcome_mock,
        mock.patch.object(
            live.legacy.coop,
            "native_party_leader",
            side_effect=(0, 1),
        ),
        mock.patch.object(
            live.legacy.coop,
            "native_leader_role_transfer_committed",
            return_value=True,
        ),
        mock.patch.object(live.session_state, "save_identity"),
        mock.patch.object(builtins, "print"),
    ):
        assert runtime.select_p1_leader(0xA11CE) == 1
        assert runtime.p1_leader_handoff_sequence == 77
        request_mock.assert_called_once_with(
            runtime.process,
            1,
            old_identity,
            runtime.session_snapshot.current_layout,
            runtime.session_snapshot.ui_layout,
            manual_control_mask=3,
        )
        runtime.maintain_p1_leader_handoff(now=float("inf"))

    outcome_mock.assert_called_once_with(runtime.process, 77)
    assert active_mock.call_args_list == [mock.call(runtime.process, False)]
    assert runtime.p1_leader_handoff_sequence == 0
    assert runtime.p1_leader_handoff_member_id is None
    assert runtime.role_generation == 5
    assert runtime.last_actor_refresh == 0.0


def test_p1_leader_input_during_closed_movement_fence_is_a_clean_rejection():
    actor = 0x1000
    battle_keep = 0x2000

    class LeaderProcess:
        def u64(self, address):
            return {
                actor + 0x698: battle_keep,
                battle_keep + 0x64: 0,
                actor: 0,
            }.get(address, 0)

        def u32(self, address):
            return 100 if address == battle_keep + 0x48 else 0

    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.process = LeaderProcess()
    runtime.active_party = [(1, "Ashe", 0xA11CE, actor)]
    runtime.p1_leader_handoff_sequence = 0
    runtime.p1_leader_handoff_member_id = None
    runtime.session_snapshot = SimpleNamespace(
        identity=live.session_state.SessionIdentity(0, 1, 2),
        current_layout=(0, 1, 2, 0xFFFF),
        ui_layout=(0, 1, 2, 0xFFFF),
    )

    with (
        mock.patch.object(
            live.legacy.coop,
            "request_native_leader_role_transfer",
            side_effect=live.legacy.coop.NativeLeaderRoleAdmissionClosed(
                "movement is fenced"
            ),
        ) as request,
        mock.patch.object(live.legacy.coop, "read_manual_control_mask", return_value=3),
        mock.patch.object(live.legacy.coop, "native_party_leader", return_value=0),
        mock.patch.object(builtins, "print") as output,
    ):
        assert runtime.select_p1_leader(0xA11CE) == 0

    request.assert_called_once()
    assert runtime.p1_leader_handoff_sequence == 0
    assert any(
        "movement-restarting" in str(call.args[0])
        for call in output.call_args_list
        if call.args
    )


def test_completed_leader_handoff_clear_lands_then_raises_is_locally_terminal():
    class LandingProcess:
        def __init__(self):
            self.cleared = False

        def write(self, address, payload):
            assert address == (
                live.legacy.coop.P2_DEVICE_FILTER_STATE
                + live.legacy.coop.NATIVE_LEADER_ROLE_ACTIVE_OFFSET
            )
            self.cleared = bytes(payload) == b"\x00\x00\x00\x00"
            raise OSError("clear landed before failure was reported")

    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.process = LandingProcess()
    runtime.p1_leader_handoff_sequence = 77
    runtime.p1_leader_handoff_member_id = 1
    runtime.p1_leader_handoff_deadline = float("inf")
    runtime.p1_leader_handoff_identity = live.session_state.SessionIdentity(1, 0, 2)
    runtime.p1_leader_handoff_identity_saved = True
    runtime.p1_leader_handoff_save_retry_at = 0.0
    runtime.role_generation = 4
    runtime.last_actor_refresh = 1.0

    with (
        mock.patch.object(
            live.legacy.coop,
            "native_leader_role_transfer_outcome",
            return_value="applied",
        ),
        mock.patch.object(
            live.legacy.coop,
            "native_leader_role_transfer_committed",
            return_value=True,
        ),
        mock.patch.object(builtins, "print"),
    ):
        runtime.maintain_p1_leader_handoff(now=1.0)

    assert runtime.process.cleared
    assert runtime.p1_leader_handoff_sequence == 0
    assert runtime.p1_leader_handoff_member_id is None
    assert runtime.role_generation == 5
    assert runtime.last_actor_refresh == 0.0


def test_movement_owned_prepared_cancel_ack_clears_headless_pending_state():
    runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    runtime.process = mock.sentinel.process
    runtime.p1_leader_handoff_sequence = 77
    runtime.p1_leader_handoff_member_id = 1
    runtime.p1_leader_handoff_deadline = float("inf")
    runtime.p1_leader_handoff_identity = live.session_state.SessionIdentity(1, 0, 2)
    runtime.p1_leader_handoff_identity_saved = False
    runtime.p1_leader_handoff_save_retry_at = 0.0
    runtime.role_generation = 4
    runtime.last_actor_refresh = 1.0

    with (
        mock.patch.object(
            live.legacy.coop,
            "native_leader_role_transfer_outcome",
            return_value="cancelled",
        ),
        mock.patch.object(
            live.legacy.coop,
            "acknowledge_native_leader_role_cancellation",
            return_value=77,
        ) as acknowledge,
        mock.patch.object(live.session_state, "save_identity") as save_identity,
        mock.patch.object(builtins, "print"),
    ):
        runtime.maintain_p1_leader_handoff(now=1.0)

    acknowledge.assert_called_once_with(runtime.process, 77)
    save_identity.assert_not_called()
    assert runtime.p1_leader_handoff_sequence == 0
    assert runtime.p1_leader_handoff_member_id is None
    assert runtime.p1_leader_handoff_identity is None
    assert runtime.role_generation == 5
    assert runtime.last_actor_refresh == 0.0


def test_m2_m3_and_pointer_axes_keep_the_stock_native_boundary():
    bridge = live.NativeDialogInputBridge(MemoryProcess(), 0x500000)
    button = bridge._build_desktop_button_cave()
    assert button.startswith(live.NATIVE_DESKTOP_BUTTON_ORIGINAL + b"\x85\xd2\x0f\x85")
    # EDX==0 (M1) alone reaches the helper.  M2/M3 jump directly to the exact
    # native return; camera/pointer axes are not patched anywhere by this bridge.
    branch_at = len(live.NATIVE_DESKTOP_BUTTON_ORIGINAL) + 2
    displacement = struct.unpack_from("<i", button, branch_at + 2)[0]
    branch_target = branch_at + 6 + displacement
    helper_call = button.index(b"\xe8", branch_at + 6)
    assert branch_target > helper_call
    assert {live.NATIVE_DESKTOP_BUTTON_SITE, live.NATIVE_DESKTOP_WHEEL_SITE}.isdisjoint(
        {0x7A30BB}
    )
