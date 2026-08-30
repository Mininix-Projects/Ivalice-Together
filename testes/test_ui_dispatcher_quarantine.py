from __future__ import annotations

import struct
import unittest
from unittest import mock

import headless_menu_core as menu_core
import headless_menu_shell as menu_shell
import native_area_target_circle_controller as area_circle
import native_target_circle_controller as target_controller
import native_target_circle_router as target_router


class MemoryProcess:
    def __init__(self, *, allocation: int = 0x60000000) -> None:
        self.memory: dict[int, int] = {}
        self.mutations: list[tuple[str, int, bytes]] = []
        self.allocation = allocation
        self.forced_state_address = 0
        self.forced_state: int | None = None
        self.seed(menu_core.UI_MANAGER_TICK_CALL, menu_core.ORIGINAL_TICK_CALL)
        self.seed(
            target_router.TARGET_PALETTE_COMMAND_LOAD_SITE,
            target_router.TARGET_PALETTE_COMMAND_LOAD_ORIGINAL,
        )
        self.seed(
            target_router.TARGET_PALETTE_FIELD_LOAD_SITE,
            target_router.TARGET_PALETTE_FIELD_LOAD_ORIGINAL,
        )
        self.seed(
            target_router.TARGET_PALETTE_CREATE_LOAD_SITE,
            target_router.TARGET_PALETTE_CREATE_LOAD_ORIGINAL,
        )

    def seed(self, address: int, data: bytes) -> None:
        for offset, value in enumerate(data):
            self.memory[address + offset] = value

    def read(self, address: int, size: int) -> bytes:
        return bytes(self.memory.get(address + offset, 0) for offset in range(size))

    def write(self, address: int, data: bytes | bytearray) -> None:
        payload = bytes(data)
        self.mutations.append(("write", address, payload))
        self.seed(address, payload)

    def patch_code(self, address: int, data: bytes) -> None:
        payload = bytes(data)
        self.mutations.append(("patch", address, payload))
        self.seed(address, payload)

    def u32(self, address: int) -> int:
        observed = struct.unpack("<I", self.read(address, 4))[0]
        if (
            address == self.forced_state_address
            and self.forced_state is not None
            and observed in (1, 2, 3, 4)
        ):
            return self.forced_state
        return observed

    def u64(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]

    def ensure_damage_number_theme_allocation(self) -> int:
        return self.allocation

    def ensure_cave_allocation(self, *, allow_allocate: bool = True) -> int:
        del allow_allocate
        return 0x50000000


class UiDispatcherQuarantineTests(unittest.TestCase):
    def _assert_quarantine_lifecycle(
        self,
        *,
        process: MemoryProcess,
        dispatcher,
        invoke,
        module,
        dispatch_state: int,
    ) -> None:
        process.forced_state_address = dispatcher.state
        process.forced_state = dispatch_state

        with mock.patch.object(module.time, "monotonic", side_effect=(0.0, 3.0)):
            with self.assertRaises(RuntimeError):
                invoke()

        mux = getattr(dispatcher, "mux", None)
        if mux is None:
            mux = dispatcher._topology.mux
        expected_root = mux.patch
        self.assertEqual(
            process.read(menu_core.UI_MANAGER_TICK_CALL, 5),
            expected_root,
        )
        self.assertTrue(dispatcher.patched)
        self.assertTrue(dispatcher.quarantined)

        mailbox = process.read(dispatcher.state, 0x20)
        cave = process.read(dispatcher.cave, len(dispatcher._dispatch_code))
        mutation_count = len(process.mutations)

        with self.assertRaises(RuntimeError):
            invoke()
        self.assertFalse(dispatcher.restore())
        self.assertEqual(len(process.mutations), mutation_count)
        self.assertEqual(process.read(dispatcher.state, 0x20), mailbox)
        self.assertEqual(
            process.read(dispatcher.cave, len(dispatcher._dispatch_code)),
            cave,
        )
        self.assertEqual(
            process.read(menu_core.UI_MANAGER_TICK_CALL, 5),
            expected_root,
        )

        process.forced_state = 3 if dispatch_state == 1 else 4
        process.seed(
            dispatcher.state,
            struct.pack("<I", process.forced_state),
        )
        sequence = getattr(
            dispatcher,
            "_pending_sequence",
            getattr(dispatcher, "_sequence", 0),
        )
        if isinstance(dispatcher, target_router.NativeTargetCircleRetargeter):
            ack_offset = target_router.TARGET_ACK_SEQUENCE_OFFSET
        elif isinstance(
            dispatcher,
            area_circle.NativeAreaTargetCircleController,
        ):
            ack_offset = area_circle.AREA_ACK_SEQUENCE_OFFSET
        else:
            ack_offset = 0x08
        if sequence:
            process.seed(
                dispatcher.state + ack_offset,
                struct.pack("<I", sequence),
            )
        if (
            isinstance(dispatcher, target_router.NativeTargetCircleRetargeter)
            and process.u32(
                dispatcher.state + target_router.TARGET_OPERATION_OFFSET
            )
            == target_router.TARGET_OPERATION_CREATE
            and process.forced_state == 3
        ):
            # A terminal CREATE is now a durable allocation journal.  The
            # callback publishes its effective heap identity and result before
            # state 3; a bare status/ack pair is deliberately invalid.
            target = process.u32(
                dispatcher.state + target_router.TARGET_HANDLE_OFFSET
            )
            process.seed(
                dispatcher.state + target_router.TARGET_NODE_OFFSET,
                struct.pack("<Q", 0x70000000),
            )
            process.seed(
                dispatcher.state + target_router.TARGET_OBSERVED_OFFSET,
                struct.pack("<I", target),
            )
            process.seed(
                dispatcher.state + target_router.TARGET_OBSERVED_PALETTE_OFFSET,
                struct.pack("<I", 4),
            )
            process.seed(
                dispatcher.state + target_router.TARGET_FACTORY_ENTERED_OFFSET,
                struct.pack("<I", 1),
            )
            process.seed(
                dispatcher.state + target_router.TARGET_RESULT_MANAGER_OFFSET,
                struct.pack("<Q", 0x71000000),
            )
            process.seed(
                dispatcher.state + target_router.TARGET_RESULT_ALLOCATOR_OFFSET,
                struct.pack("<Q", 0x72000000),
            )
        elif (
            isinstance(dispatcher, target_router.NativeTargetCircleRetargeter)
            and process.forced_state == 3
        ):
            # Simulate the authenticated callback's atomic UPDATE publication:
            # native result fields and the exact resident sidecar change
            # together before terminal state 3 becomes visible.
            owner = process.u32(
                dispatcher.state + target_router.TARGET_OWNER_OFFSET
            )
            token = process.u32(
                dispatcher.state + target_router.TARGET_AUTH_TOKEN_OFFSET
            )
            manager = process.u64(
                dispatcher.state + target_router.TARGET_EXPECTED_MANAGER_OFFSET
            )
            allocator = process.u64(
                dispatcher.state
                + target_router.TARGET_EXPECTED_ALLOCATOR_OFFSET
            )
            node = process.u64(
                dispatcher.state + target_router.TARGET_NODE_OFFSET
            )
            target = process.u32(
                dispatcher.state + target_router.TARGET_HANDLE_OFFSET
            )
            palette = process.u32(
                dispatcher.state + target_router.TARGET_DESIRED_PALETTE_OFFSET
            )
            updated = target_router.TargetCircleOwnership(
                owner_tag=owner,
                token=token,
                manager=manager,
                allocator=allocator,
                node=node,
                target=target,
                palette=palette,
            )
            slot = target_router.TARGET_OWNER_TAGS.index(owner)
            process.seed(
                dispatcher.state
                + target_router.TARGET_OWNERSHIP_RECORDS_OFFSET
                + slot * target_router.TARGET_OWNERSHIP_RECORD_SIZE,
                target_router._pack_ownership_record(updated),
            )
            process.seed(
                dispatcher.state + target_router.TARGET_OBSERVED_OFFSET,
                struct.pack("<I", target),
            )
            process.seed(
                dispatcher.state + target_router.TARGET_OBSERVED_PALETTE_OFFSET,
                struct.pack("<I", palette),
            )
        self.assertTrue(dispatcher.maintain())
        self.assertFalse(dispatcher.patched)
        self.assertFalse(dispatcher.quarantined)
        self.assertEqual(
            process.read(menu_core.UI_MANAGER_TICK_CALL, 5),
            expected_root,
        )

    def test_action_submitter_quarantines_armed_and_claimed_timeout(self) -> None:
        for dispatch_state in (1, 2):
            with self.subTest(dispatch_state=dispatch_state):
                process = MemoryProcess()
                dispatcher = menu_core.NativeActionSubmitter(process, 0x50000000)
                self._assert_quarantine_lifecycle(
                    process=process,
                    dispatcher=dispatcher,
                    invoke=lambda: dispatcher.submit(0x11, 0x22, 0x33),
                    module=menu_core,
                    dispatch_state=dispatch_state,
                )

    def test_combat_indicator_quarantines_armed_and_claimed_timeout(self) -> None:
        for dispatch_state in (1, 2):
            with self.subTest(dispatch_state=dispatch_state):
                process = MemoryProcess()
                dispatcher = menu_core.NativeCombatIndicatorRouter(
                    process,
                    0x50000000,
                )
                self._assert_quarantine_lifecycle(
                    process=process,
                    dispatcher=dispatcher,
                    invoke=lambda: dispatcher.set(0x11, 0x22),
                    module=menu_core,
                    dispatch_state=dispatch_state,
                )

    def test_retargeter_quarantines_armed_and_claimed_timeout(self) -> None:
        for dispatch_state in (1, 2):
            with self.subTest(dispatch_state=dispatch_state):
                process = MemoryProcess()
                dispatcher = target_router.NativeTargetCircleRetargeter(process)
                manager = 0x71000000
                allocator = 0x72000000
                node = 0x70000000
                original_target = 0x100010
                process.seed(
                    target_router.TARGET_UI_MANAGER_GLOBAL,
                    struct.pack("<Q", manager),
                )
                process.seed(
                    manager + target_router.TARGET_CIRCLE_ALLOCATOR_OFFSET,
                    struct.pack("<Q", allocator),
                )
                process.seed(manager + 0x10F00, struct.pack("<Q", node))
                process.seed(manager + 0x10EF0, struct.pack("<Q", node))
                process.seed(node + 0xD68, b"\x00" * 8)
                process.seed(node + 0xD70, b"\x00" * 8)
                process.seed(node + 0xD54, struct.pack("<H", 2))
                process.seed(node + 0xD2C, struct.pack("<I", original_target))
                process.seed(node + 0xD7C, struct.pack("<I", 4))
                owned = target_router.TargetCircleOwnership(
                    owner_tag=target_router.P1_TARGET_OWNER_TAG,
                    token=1,
                    manager=manager,
                    allocator=allocator,
                    node=node,
                    target=original_target,
                    palette=4,
                )
                process.seed(
                    dispatcher.state
                    + target_router.TARGET_OWNERSHIP_RECORDS_OFFSET,
                    target_router._pack_ownership_record(owned),
                )
                process.seed(
                    dispatcher.state
                    + target_router.TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET,
                    struct.pack("<I", 2),
                )
                process.mutations.clear()
                self._assert_quarantine_lifecycle(
                    process=process,
                    dispatcher=dispatcher,
                    invoke=lambda: dispatcher.retarget(
                        owned,
                        0x100008,
                        palette=2,
                    ),
                    module=target_router,
                    dispatch_state=dispatch_state,
                )

    def test_target_creator_quarantines_armed_and_claimed_timeout(self) -> None:
        for dispatch_state in (1, 2):
            with self.subTest(dispatch_state=dispatch_state):
                process = MemoryProcess()
                controller = target_controller.NativeTargetCircleController(
                    process,
                    owner_tag=target_router.P2_TARGET_OWNER_TAG,
                )
                dispatcher = controller.router
                process.mutations.clear()
                self._assert_quarantine_lifecycle(
                    process=process,
                    dispatcher=dispatcher,
                    invoke=lambda: controller._create(0x100008, 4),
                    module=target_controller,
                    dispatch_state=dispatch_state,
                )

    def test_area_creator_quarantines_armed_and_claimed_timeout(self) -> None:
        for dispatch_state in (1, 2):
            with self.subTest(dispatch_state=dispatch_state):
                process = MemoryProcess()
                dispatcher = area_circle.NativeAreaTargetCircleController(
                    process,
                    owner_tag=area_circle.P2_OWNER_TAG,
                    palette=4,
                )
                process.mutations.clear()
                self._assert_quarantine_lifecycle(
                    process=process,
                    dispatcher=dispatcher,
                    invoke=lambda: dispatcher._create(0x100008, 8.0),
                    module=area_circle,
                    dispatch_state=dispatch_state,
                )

    def test_terminal_state_does_not_reclaim_a_changed_cave(self) -> None:
        process = MemoryProcess()
        dispatcher = menu_core.NativeActionSubmitter(process, 0x50000000)
        process.forced_state_address = dispatcher.state
        process.forced_state = 1
        with mock.patch.object(menu_core.time, "monotonic", side_effect=(0.0, 3.0)):
            with self.assertRaises(RuntimeError):
                dispatcher.submit(0x11, 0x22, 0x33)

        process.forced_state = 3
        process.seed(dispatcher.cave + 20, b"\xcc")
        mutations = len(process.mutations)

        self.assertFalse(dispatcher.maintain())
        self.assertEqual(len(process.mutations), mutations)
        self.assertNotEqual(
            process.read(menu_core.UI_MANAGER_TICK_CALL, 5),
            menu_core.ORIGINAL_TICK_CALL,
        )

    def test_area_dispatcher_exact_adoption_is_read_only_until_terminal(self) -> None:
        for dispatch_state in (1, 2):
            with self.subTest(dispatch_state=dispatch_state):
                process = MemoryProcess()
                owner = area_circle.NativeAreaTargetCircleController(
                    process,
                    owner_tag=area_circle.P2_OWNER_TAG,
                    palette=4,
                )
                state_image = bytearray(owner._initial_state_image())
                struct.pack_into("<I", state_image, 0, dispatch_state)
                struct.pack_into(
                    "<I",
                    state_image,
                    area_circle.AREA_SEQUENCE_OFFSET,
                    2,
                )
                struct.pack_into(
                    "<I",
                    state_image,
                    area_circle.AREA_OWNER_TAG_OFFSET,
                    area_circle.P2_OWNER_TAG,
                )
                process.seed(owner.state, state_image)
                process.mutations.clear()

                dispatcher = area_circle.NativeAreaTargetCircleController(
                    process,
                    owner_tag=area_circle.P2_OWNER_TAG,
                    palette=4,
                )

                self.assertEqual(process.mutations, [])
                self.assertTrue(dispatcher.patched)
                self.assertTrue(dispatcher.quarantined)
                self.assertFalse(dispatcher.restore())
                self.assertEqual(process.mutations, [])

                process.seed(dispatcher.state, struct.pack("<I", 3))
                process.seed(
                    dispatcher.state + area_circle.AREA_ACK_SEQUENCE_OFFSET,
                    struct.pack("<I", dispatcher._sequence),
                )
                self.assertTrue(dispatcher.maintain())
                self.assertEqual(
                    process.read(menu_core.UI_MANAGER_TICK_CALL, 5),
                    dispatcher.mux.patch,
                )

    def test_frame_maintenance_polls_every_locally_owned_dispatcher(self) -> None:
        runtime = menu_shell.CoopMenuRuntime.__new__(menu_shell.CoopMenuRuntime)
        direct = [mock.Mock(patched=True) for _ in range(5)]
        routed = [mock.Mock() for _ in range(3)]
        for owner in routed:
            owner.router = mock.Mock(patched=True)
        (
            runtime.native_submitter,
            runtime.native_combat_indicator,
            runtime.native_p1_area_circle,
            runtime.native_p2_area_circle,
            runtime.native_p3_area_circle,
        ) = direct
        (
            runtime.native_p1_target_circle,
            runtime.native_target_circle,
            runtime.native_p3_target_circle,
        ) = routed

        runtime.maintain_owned_ui_tick_dispatchers()

        for dispatcher in (*direct, *(owner.router for owner in routed)):
            dispatcher.maintain.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
