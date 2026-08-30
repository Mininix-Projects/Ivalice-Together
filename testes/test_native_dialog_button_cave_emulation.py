"""Execution-level regressions for native semantic firewall authority."""

import faulthandler
import struct

import pytest

unicorn = pytest.importorskip("unicorn")
from unicorn import UC_ARCH_X86, UC_MODE_64, Uc  # noqa: E402
from unicorn.x86_const import (  # noqa: E402
    UC_X86_REG_EAX,
    UC_X86_REG_RCX,
    UC_X86_REG_RDX,
    UC_X86_REG_RIP,
    UC_X86_REG_RSP,
)

import headless_menu_shell as live  # noqa: E402
import session_state  # noqa: E402


BASE = 0x00500000
DEVICE = 0x00600000
CONTROL = 0x00610000
STACK = 0x00700000
STOP = BASE + 0xF800


def _policy(*, fail_closed: bool) -> live.NativeInputPolicy:
    return live.native_input_policy(
        p1_replacement=fail_closed,
        non_p1_command_blocked=fail_closed,
        custom_directional_owner=False,
        p1_menu_owns_lb=False,
        party_menu_blocked=fail_closed,
        quickening_block_mask=0,
        leader_replacement=fail_closed,
        capture_desktop_events=False,
        p1_native_slot=0,
        native_gamepad_slot_mask=0x0F,
    )

def _execute_button_query(
    *,
    policy: live.NativeInputPolicy,
    button: int,
    slot: int,
    user_control_pointer: int,
    user_control_enabled: bool,
    pause_present: bool,
) -> int:
    bridge = live.NativeDialogInputBridge(object(), BASE)

    # Unicorn's Windows backend probes reservations with a handled access
    # violation.  Suppress faulthandler only around those expected probes.
    restore_faulthandler = faulthandler.is_enabled()
    if restore_faulthandler:
        faulthandler.disable()
    try:
        machine = Uc(UC_ARCH_X86, UC_MODE_64)
        machine.mem_map(BASE, 0x10000)
        machine.mem_map(DEVICE, 0x1000)
        machine.mem_map(CONTROL, 0x1000)
        machine.mem_map(STACK, 0x2000)
        machine.mem_map(0x02000000, 0x100000)
        machine.mem_map(live.NATIVE_GET_BUTTON_RETURN & ~0xFFF, 0x1000)
    finally:
        if restore_faulthandler:
            faulthandler.enable()

    machine.mem_write(bridge.cave, bridge._build_cave())
    machine.mem_write(
        live.NATIVE_GET_BUTTON_RETURN,
        b"\xB8\x01\x00\x00\x00\xC3",  # original continuation => logical down
    )
    machine.mem_write(DEVICE + 0x458, struct.pack("<I", slot))
    machine.mem_write(bridge.input_state, policy.pack())
    machine.mem_write(
        bridge.input_state + live.NATIVE_INPUT_POLICY_ACTIVE_OFFSET,
        struct.pack("<I", 2),
    )
    machine.mem_write(
        session_state.USER_CONTROL_GLOBAL,
        struct.pack("<Q", user_control_pointer),
    )
    if user_control_pointer:
        machine.mem_write(
            user_control_pointer + session_state.USER_CONTROL_ENABLED_OFFSET,
            bytes((int(user_control_enabled),)),
        )
    pause_object = CONTROL + 0x100 if pause_present else 0
    machine.mem_write(
        live.NATIVE_PAUSE_PRIMARY_OBJECT,
        struct.pack("<Q", pause_object),
    )
    machine.mem_write(live.NATIVE_PAUSE_SECONDARY_OBJECT, b"\x00" * 8)

    stack_pointer = STACK + 0x1000
    machine.mem_write(stack_pointer, struct.pack("<Q", STOP))
    machine.reg_write(UC_X86_REG_RSP, stack_pointer)
    machine.reg_write(UC_X86_REG_RCX, DEVICE)
    machine.reg_write(UC_X86_REG_RDX, button)
    machine.emu_start(bridge.cave, STOP, count=500)

    assert machine.reg_read(UC_X86_REG_RIP) == STOP
    return machine.reg_read(UC_X86_REG_EAX)


@pytest.mark.parametrize(
    "button,slot,user_control_pointer,pause_present",
    (
        (live.NATIVE_DIALOG_UP, 0, 0, False),
        (live.NATIVE_DIALOG_COMMAND, 0, CONTROL, False),
        (live.NATIVE_DIALOG_COMMAND, 1, CONTROL, False),
        (live.NATIVE_DIALOG_UP, 1, CONTROL, True),
    ),
    ids=("leader-null-ucon", "p1-command-ucoff", "p2-command-ucoff", "leader-pre-ack-pause"),
)
def test_fail_closed_policy_remains_authoritative_when_ucon_is_off(
    button,
    slot,
    user_control_pointer,
    pause_present,
):
    assert (
        _execute_button_query(
            policy=_policy(fail_closed=True),
            button=button,
            slot=slot,
            user_control_pointer=user_control_pointer,
            user_control_enabled=False,
            pause_present=pause_present,
        )
        == 0
    )


def test_acknowledged_pause_all_native_policy_reaches_original_query():
    assert (
        _execute_button_query(
            policy=_policy(fail_closed=False),
            button=live.NATIVE_DIALOG_UP,
            slot=0,
            user_control_pointer=CONTROL,
            user_control_enabled=False,
            pause_present=True,
        )
        == 1
    )
