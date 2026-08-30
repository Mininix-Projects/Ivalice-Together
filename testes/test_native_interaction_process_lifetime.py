"""Transactional/process-lifetime interaction-hook regressions."""

import struct
import unittest

import coop_runtime as runtime
import headless_menu_shell as menu


class TransactionProcess:
    BASE = 0x5A000000

    def __init__(self) -> None:
        self.memory: dict[int, int] = {}
        self.operations: list[tuple[str, int, bytes]] = []
        self.writes: list[tuple[int, bytes]] = []
        self.patches: list[tuple[int, bytes]] = []
        self.operation_count = 0
        self.fail_operation: int | None = None
        self.fail_after_mutation = False
        self.seed(
            menu.NATIVE_INTERACTION_CONSUMER_CALL,
            menu.NATIVE_INTERACTION_CONSUMER_CALL_ORIGINAL,
        )
        for site, original in (
            (
                runtime.FOLLOWER_NPC_ENTRY_GUARD_SITE,
                runtime.FOLLOWER_NPC_ENTRY_GUARD_ORIGINAL,
            ),
            (
                runtime.FOLLOWER_NPC_ACCEPT_GUARD_SITE,
                runtime.FOLLOWER_NPC_ACCEPT_GUARD_ORIGINAL,
            ),
            (
                runtime.FOLLOWER_NPC_CLEANUP_GUARD_SITE,
                runtime.FOLLOWER_NPC_CLEANUP_GUARD_ORIGINAL,
            ),
        ):
            self.seed(site, original)

    def seed(self, address: int, payload: bytes) -> None:
        for offset, value in enumerate(bytes(payload)):
            self.memory[int(address) + offset] = value

    def read(self, address: int, size: int) -> bytes:
        return bytes(
            self.memory.get(int(address) + offset, 0)
            for offset in range(int(size))
        )

    def u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]

    def write(self, address: int, payload: bytes) -> None:
        self._mutate("write", address, payload, self.writes)

    def patch_code(self, address: int, payload: bytes) -> None:
        self._mutate("patch", address, payload, self.patches)

    def _mutate(
        self,
        kind: str,
        address: int,
        payload: bytes,
        journal: list[tuple[int, bytes]],
    ) -> None:
        address = int(address)
        payload = bytes(payload)
        self.operation_count += 1
        must_fail = self.operation_count == self.fail_operation
        if must_fail and not self.fail_after_mutation:
            raise OSError(f"injected {kind} failure before mutation")
        journal.append((address, payload))
        self.operations.append((kind, address, payload))
        self.seed(address, payload)
        if must_fail:
            raise OSError(f"injected {kind} failure after mutation")

    def arm_failure(self, operation: int, *, after_mutation: bool) -> None:
        self.operation_count = 0
        self.fail_operation = int(operation)
        self.fail_after_mutation = bool(after_mutation)

    def clear_activity(self) -> None:
        self.operations.clear()
        self.writes.clear()
        self.patches.clear()

    def ensure_dual_interaction_allocation(self) -> int:
        runtime.set_dual_interaction_base(self.BASE)
        return self.BASE


class NullBridge:
    def release(self) -> None:
        return None


class FollowerNpcGuardProcessLifetimeTests(unittest.TestCase):
    ACTIVE_STATE = TransactionProcess.BASE + 0xE350

    def specs(self):
        runtime.set_dual_interaction_base(TransactionProcess.BASE)
        return runtime._follower_npc_private_object_guard_specs(self.ACTIVE_STATE)

    def test_failure_before_and_after_every_write_and_root_is_one_way(self):
        for operation in range(1, 7):
            for after_mutation in (False, True):
                with self.subTest(
                    operation=operation,
                    after_mutation=after_mutation,
                ):
                    process = TransactionProcess()
                    specs = self.specs()
                    process.arm_failure(
                        operation,
                        after_mutation=after_mutation,
                    )

                    with self.assertRaises(OSError):
                        runtime.install_follower_npc_private_object_guards(
                            process,
                            active_state_address=self.ACTIVE_STATE,
                        )

                    recorded = operation if after_mutation else operation - 1
                    self.assertEqual(len(process.operations), recorded)
                    for index, (site, _cave, original, _image, patch) in enumerate(
                        specs
                    ):
                        boundary = 4 + index
                        committed = operation > boundary or (
                            operation == boundary and after_mutation
                        )
                        self.assertEqual(
                            process.read(site, len(original)),
                            patch if committed else original,
                        )
                        self.assertNotIn(("patch", site, original), process.operations)
                    if any(kind == "patch" for kind, _address, _data in process.operations):
                        for _site, cave, _original, image, _patch in specs:
                            self.assertEqual(process.read(cave, len(image)), image)

    def test_prepared_or_partial_helper_topology_is_rejected_read_only(self):
        for operation, after_mutation in ((3, True), (5, False)):
            with self.subTest(
                operation=operation,
                after_mutation=after_mutation,
            ):
                process = TransactionProcess()
                self.specs()
                process.arm_failure(operation, after_mutation=after_mutation)
                with self.assertRaises(OSError):
                    runtime.install_follower_npc_private_object_guards(
                        process,
                        active_state_address=self.ACTIVE_STATE,
                    )
                expected_memory = dict(process.memory)
                process.clear_activity()

                with self.assertRaises(RuntimeError):
                    runtime.install_follower_npc_private_object_guards(
                        process,
                        active_state_address=self.ACTIVE_STATE,
                    )

                self.assertEqual(process.memory, expected_memory)
                self.assertEqual(process.operations, [])

    def test_foreign_helper_root_is_rejected_before_any_mutation(self):
        process = TransactionProcess()
        self.specs()
        process.seed(
            runtime.FOLLOWER_NPC_ENTRY_GUARD_SITE,
            b"\xCC" * len(runtime.FOLLOWER_NPC_ENTRY_GUARD_ORIGINAL),
        )
        expected_memory = dict(process.memory)
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "partial/foreign"):
            runtime.install_follower_npc_private_object_guards(
                process,
                active_state_address=self.ACTIVE_STATE,
            )

        self.assertEqual(process.memory, expected_memory)
        self.assertEqual(process.operations, [])

    def test_exception_after_final_root_is_adopted_read_only(self):
        process = TransactionProcess()
        self.specs()
        process.arm_failure(6, after_mutation=True)
        with self.assertRaises(OSError):
            runtime.install_follower_npc_private_object_guards(
                process,
                active_state_address=self.ACTIVE_STATE,
            )
        expected_memory = dict(process.memory)
        process.clear_activity()

        installed = runtime.install_follower_npc_private_object_guards(
            process,
            active_state_address=self.ACTIVE_STATE,
        )

        self.assertFalse(installed)
        self.assertEqual(process.memory, expected_memory)
        self.assertEqual(process.operations, [])

    def test_active_helper_validates_the_entire_reserved_cave_slot(self):
        process = TransactionProcess()
        specs = self.specs()
        runtime.install_follower_npc_private_object_guards(
            process,
            active_state_address=self.ACTIVE_STATE,
        )
        _site, cave, _original, image, _patch = specs[-1]
        process.seed(cave + len(image) - 1, b"\xCC")
        expected_memory = dict(process.memory)
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "guard cave mismatch"):
            runtime.install_follower_npc_private_object_guards(
                process,
                active_state_address=self.ACTIVE_STATE,
            )

        self.assertEqual(process.memory, expected_memory)
        self.assertEqual(process.operations, [])

    def test_restore_keeps_static_topology_and_only_disarms_exact_state(self):
        process = TransactionProcess()
        specs = self.specs()
        runtime.install_follower_npc_private_object_guards(
            process,
            active_state_address=self.ACTIVE_STATE,
        )
        static_snapshot = tuple(
            (
                process.read(site, len(original)),
                process.read(cave, len(image)),
            )
            for site, cave, original, image, _patch in specs
        )
        process.seed(self.ACTIVE_STATE, b"\x01\x00\x00\x00")
        process.clear_activity()

        self.assertFalse(
            runtime.restore_follower_npc_private_object_guards(
                process,
                active_state_address=self.ACTIVE_STATE + 4,
            )
        )
        self.assertEqual(process.operations, [])

        restored = runtime.restore_follower_npc_private_object_guards(
            process,
            active_state_address=self.ACTIVE_STATE,
        )

        self.assertTrue(restored)
        self.assertEqual(process.read(self.ACTIVE_STATE, 4), b"\x00" * 4)
        self.assertEqual(
            tuple(
                (
                    process.read(site, len(original)),
                    process.read(cave, len(image)),
                )
                for site, cave, original, image, _patch in specs
            ),
            static_snapshot,
        )
        self.assertEqual(
            process.operations,
            [("write", self.ACTIVE_STATE, b"\x00" * 4)],
        )


class NativeInteractionRouterProcessLifetimeTests(unittest.TestCase):
    def topology(self):
        runtime.set_dual_interaction_base(TransactionProcess.BASE)
        mailbox = (
            TransactionProcess.BASE + menu.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET
        )
        active_state = mailbox + menu.P2_INTERACTION_CONSUMER_EXACT_SCAN_ACTIVE_OFFSET
        guard_specs = runtime._follower_npc_private_object_guard_specs(active_state)
        consumer_cave = (
            TransactionProcess.BASE + menu.P2_INTERACTION_CONSUMER_CAVE_OFFSET
        )
        atomic_cave = TransactionProcess.BASE + menu.FOLLOWER_NPC_ATOMIC_CAVE_OFFSET
        wrapper = menu.build_p2_interaction_consumer_wrapper(mailbox, atomic_cave)
        atomic = menu.build_atomic_follower_npc_interaction_cave(
            atomic_cave,
            mailbox,
        )
        consumer_patch = b"\xe8" + struct.pack(
            "<i",
            consumer_cave - (menu.NATIVE_INTERACTION_CONSUMER_CALL + 5),
        )
        return {
            "mailbox": mailbox,
            "active_state": active_state,
            "guard_specs": guard_specs,
            "consumer_cave": consumer_cave,
            "consumer_image": wrapper.ljust(
                menu.P2_INTERACTION_CONSUMER_CAVE_LIMIT,
                b"\x00",
            ),
            "atomic_cave": atomic_cave,
            "atomic_image": atomic.ljust(menu.FOLLOWER_NPC_ATOMIC_CAVE_LIMIT, b"\x00"),
            "consumer_patch": consumer_patch,
        }

    def assert_complete_static_images(self, process, topology) -> None:
        mailbox_image = menu.P2_INTERACTION_CONSUMER_MAGIC.ljust(
            menu.P2_INTERACTION_CONSUMER_MAILBOX_SIZE,
            b"\x00",
        )
        self.assertEqual(
            process.read(topology["mailbox"], len(mailbox_image)),
            mailbox_image,
        )
        for address, image in (
            (topology["consumer_cave"], topology["consumer_image"]),
            (topology["atomic_cave"], topology["atomic_image"]),
            *(
                (cave, image)
                for _site, cave, _original, image, _patch in topology["guard_specs"]
            ),
        ):
            self.assertEqual(process.read(address, len(image)), image)

    def static_snapshot(self, process, topology):
        roots = (
            (
                menu.NATIVE_INTERACTION_CONSUMER_CALL,
                len(menu.NATIVE_INTERACTION_CONSUMER_CALL_ORIGINAL),
            ),
            *(
                (site, len(original))
                for site, _cave, original, _image, _patch in topology["guard_specs"]
            ),
        )
        caves = (
            (topology["consumer_cave"], len(topology["consumer_image"])),
            (topology["atomic_cave"], len(topology["atomic_image"])),
            *(
                (cave, len(image))
                for _site, cave, _original, image, _patch in topology["guard_specs"]
            ),
        )
        return (
            tuple(process.read(address, size) for address, size in roots),
            tuple(process.read(address, size) for address, size in caves),
        )

    def test_failure_before_and_after_every_boundary_never_rolls_back(self):
        for operation in range(1, 11):
            for after_mutation in (False, True):
                with self.subTest(
                    operation=operation,
                    after_mutation=after_mutation,
                ):
                    process = TransactionProcess()
                    topology = self.topology()
                    process.arm_failure(
                        operation,
                        after_mutation=after_mutation,
                    )

                    with self.assertRaises(OSError):
                        menu.NativeWorldInteractionRouter(process, NullBridge())

                    recorded = operation if after_mutation else operation - 1
                    self.assertEqual(len(process.operations), recorded)
                    root_specs = (
                        *topology["guard_specs"],
                        (
                            menu.NATIVE_INTERACTION_CONSUMER_CALL,
                            topology["consumer_cave"],
                            menu.NATIVE_INTERACTION_CONSUMER_CALL_ORIGINAL,
                            topology["consumer_image"],
                            topology["consumer_patch"],
                        ),
                    )
                    for index, (site, _cave, original, _image, patch) in enumerate(
                        root_specs
                    ):
                        boundary = 7 + index
                        committed = operation > boundary or (
                            operation == boundary and after_mutation
                        )
                        self.assertEqual(
                            process.read(site, len(original)),
                            patch if committed else original,
                        )
                        self.assertNotIn(("patch", site, original), process.operations)
                    if any(kind == "patch" for kind, _address, _data in process.operations):
                        self.assert_complete_static_images(process, topology)

    def test_prepared_and_partial_router_topologies_abort_read_only(self):
        for operation, after_mutation in ((6, True), (10, False)):
            with self.subTest(
                operation=operation,
                after_mutation=after_mutation,
            ):
                process = TransactionProcess()
                self.topology()
                process.arm_failure(operation, after_mutation=after_mutation)
                with self.assertRaises(OSError):
                    menu.NativeWorldInteractionRouter(process, NullBridge())
                expected_memory = dict(process.memory)
                process.clear_activity()

                with self.assertRaises(RuntimeError):
                    menu.NativeWorldInteractionRouter(process, NullBridge())

                self.assertEqual(process.memory, expected_memory)
                self.assertEqual(process.operations, [])

    def test_foreign_consumer_root_is_rejected_before_any_mutation(self):
        process = TransactionProcess()
        self.topology()
        process.seed(
            menu.NATIVE_INTERACTION_CONSUMER_CALL,
            b"\xCC" * len(menu.NATIVE_INTERACTION_CONSUMER_CALL_ORIGINAL),
        )
        expected_memory = dict(process.memory)
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "partial/foreign"):
            menu.NativeWorldInteractionRouter(process, NullBridge())

        self.assertEqual(process.memory, expected_memory)
        self.assertEqual(process.operations, [])

    def test_exception_after_final_root_hot_adopts_without_writes(self):
        process = TransactionProcess()
        self.topology()
        process.arm_failure(10, after_mutation=True)
        with self.assertRaises(OSError):
            menu.NativeWorldInteractionRouter(process, NullBridge())
        expected_memory = dict(process.memory)
        process.clear_activity()

        adopted = menu.NativeWorldInteractionRouter(process, NullBridge())

        self.assertTrue(adopted.consumer_installed)
        self.assertFalse(adopted.consumer_static_owned)
        self.assertTrue(adopted.atomic_npc_guards_installed)
        self.assertFalse(adopted.atomic_npc_guards_owned)
        self.assertEqual(process.memory, expected_memory)
        self.assertEqual(process.operations, [])

    def test_active_router_validates_padding_in_the_complete_cave_slot(self):
        process = TransactionProcess()
        topology = self.topology()
        menu.NativeWorldInteractionRouter(process, NullBridge())
        address = topology["consumer_cave"] + len(topology["consumer_image"]) - 1
        process.seed(address, b"\xCC")
        expected_memory = dict(process.memory)
        process.clear_activity()

        with self.assertRaisesRegex(RuntimeError, "wrapper cave mismatch"):
            menu.NativeWorldInteractionRouter(process, NullBridge())

        self.assertEqual(process.memory, expected_memory)
        self.assertEqual(process.operations, [])

    def test_shutdown_keeps_static_topology_and_only_disarms_dynamic_state(self):
        process = TransactionProcess()
        topology = self.topology()
        router = menu.NativeWorldInteractionRouter(process, NullBridge())
        static_before = self.static_snapshot(process, topology)
        owner = runtime.COMMAND_BLOCK + menu.P2_INTERACTION_OWNER_OFFSET
        process.seed(owner, bytes((menu.INTERACTION_OWNER_P2,)))
        process.seed(topology["active_state"], b"\x01\x00\x00\x00")
        router.interaction_owner = menu.INTERACTION_OWNER_P2
        process.clear_activity()

        router.shutdown()

        self.assertEqual(self.static_snapshot(process, topology), static_before)
        self.assertEqual(process.patches, [])
        self.assertEqual(
            process.operations,
            [
                ("write", owner, bytes((menu.INTERACTION_OWNER_NONE,))),
                ("write", topology["active_state"], b"\x00" * 4),
            ],
        )
        self.assertFalse(router.consumer_installed)
        self.assertFalse(router.atomic_npc_guards_installed)

    def test_armed_one_and_two_remain_quarantined_on_shutdown(self):
        for armed in (1, 2):
            with self.subTest(armed=armed):
                process = TransactionProcess()
                topology = self.topology()
                menu.NativeWorldInteractionRouter(process, NullBridge())
                process.seed(
                    topology["mailbox"] + menu.P2_INTERACTION_CONSUMER_ARMED_OFFSET,
                    struct.pack("<I", armed),
                )
                process.seed(
                    topology["mailbox"] + menu.P2_INTERACTION_CONSUMER_DONE_OFFSET,
                    b"\x00" * 4,
                )
                adopted = menu.NativeWorldInteractionRouter(process, NullBridge())
                static_before = self.static_snapshot(process, topology)
                process.clear_activity()

                adopted.shutdown()

                self.assertTrue(adopted.consumer_quarantined)
                self.assertTrue(adopted.consumer_installed)
                self.assertEqual(process.operations, [])
                self.assertEqual(self.static_snapshot(process, topology), static_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
