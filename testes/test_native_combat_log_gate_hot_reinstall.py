"""Hot-reinstall regressions for the native Combat Log target-info gate."""

import unittest

import headless_menu_shell as gate_module


class CountingGateProcess:
    def __init__(self) -> None:
        self.memory: dict[int, int] = {}
        self.writes: list[tuple[int, bytes]] = []
        self.patches: list[tuple[int, bytes]] = []
        self.operations: list[tuple[str, int, bytes]] = []
        self.fail_reads: set[int] = set()
        self.operation_count = 0
        self.fail_operation: int | None = None
        self.fail_after_mutation = False
        self.seed(
            gate_module.NATIVE_COMBAT_LOG_TICK_CALL_SITE,
            gate_module.NATIVE_COMBAT_LOG_TICK_CALL_ORIGINAL,
        )
        self.seed(
            gate_module.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE,
            gate_module.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_ORIGINAL,
        )
        for site, original, _cave_offset in (
            gate_module.NATIVE_COMBAT_LOG_LAYOUT_GATE_SITES
        ):
            self.seed(site, original)

    def seed(self, address: int, payload: bytes) -> None:
        for offset, value in enumerate(bytes(payload)):
            self.memory[int(address) + offset] = value

    def read(self, address: int, size: int) -> bytes:
        if int(address) in self.fail_reads:
            raise OSError(f"injected read failure at {address:#x}")
        return bytes(
            self.memory.get(int(address) + offset, 0)
            for offset in range(int(size))
        )

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
        payload = bytes(payload)
        address = int(address)
        self.operation_count += 1
        must_fail = self.operation_count == self.fail_operation
        if must_fail and not self.fail_after_mutation:
            raise OSError(f"injected {kind} failure before operation")
        journal.append((address, payload))
        self.operations.append((kind, address, payload))
        self.seed(address, payload)
        if must_fail:
            raise OSError(f"injected {kind} failure after operation")

    def arm_failure(self, operation: int, *, after_mutation: bool) -> None:
        self.operation_count = 0
        self.fail_operation = int(operation)
        self.fail_after_mutation = bool(after_mutation)

    def clear_activity(self) -> None:
        self.writes.clear()
        self.patches.clear()
        self.operations.clear()


class NativeCombatLogGateHotReinstallTests(unittest.TestCase):
    BASE = 0x50000000

    def make_gate(self, process: CountingGateProcess):
        return gate_module.NativeCombatLogTargetInfoGate(process, self.BASE)

    def test_fresh_install_commits_complete_images_and_owns_static_code(self):
        process = CountingGateProcess()
        gate = self.make_gate(process)

        gate.install()

        self.assertTrue(gate.installed)
        self.assertTrue(gate.owns_static)
        self.assertFalse(gate.requested_hidden)
        self.assertEqual(
            process.read(gate_module.NATIVE_COMBAT_LOG_TICK_CALL_SITE, 5),
            gate._tick_patch(),
        )
        self.assertEqual(
            process.read(gate_module.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE, 5),
            gate._show_patch(),
        )
        self.assertEqual(
            process.read(
                gate.state + gate.MAGIC_OFFSET,
                len(gate_module.NATIVE_COMBAT_LOG_GATE_MAGIC),
            ),
            gate_module.NATIVE_COMBAT_LOG_GATE_MAGIC,
        )
        self.assertEqual(
            {address for address, _payload in process.patches},
            {
                gate.cave,
                gate.show_cave,
                gate_module.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE,
                gate_module.NATIVE_COMBAT_LOG_TICK_CALL_SITE,
                *{
                    site
                    for site, _original, _cave_offset in (
                        gate_module.NATIVE_COMBAT_LOG_LAYOUT_GATE_SITES
                    )
                },
            },
        )

    def test_exact_adoption_is_read_only_and_preserves_dynamic_state(self):
        process = CountingGateProcess()
        owner = self.make_gate(process)
        owner.install()
        owner.request_hidden(True)
        process.write(owner.state + owner.APPLIED_OFFSET, b"\x01")
        state_size = (
            gate_module.NATIVE_COMBAT_LOG_GATE_REGION_END
            - gate_module.NATIVE_COMBAT_LOG_GATE_STATE_OFFSET
        )
        expected_state = process.read(owner.state, state_size)
        expected_memory = dict(process.memory)
        process.clear_activity()

        adopted = self.make_gate(process)
        adopted.install()

        self.assertTrue(adopted.installed)
        self.assertFalse(adopted.owns_static)
        self.assertTrue(adopted.requested_hidden)
        self.assertEqual(process.read(adopted.state, state_size), expected_state)
        self.assertEqual(process.memory, expected_memory)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_resident_restore_restarts_via_read_only_adoption(self):
        process = CountingGateProcess()
        owner = self.make_gate(process)
        owner.install()
        process.clear_activity()
        self.assertTrue(owner.restore(timeout=0))
        self.assertFalse(owner.installed)
        self.assertFalse(owner.owns_static)
        expected_cave = process.read(
            owner.cave,
            gate_module.NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET
            - gate_module.NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET,
        )
        expected_show_cave = process.read(
            owner.show_cave,
            gate_module.NATIVE_COMBAT_LOG_GATE_STATE_OFFSET
            - gate_module.NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET,
        )
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])
        process.clear_activity()

        restarted = self.make_gate(process)
        restarted.install()

        self.assertTrue(restarted.installed)
        self.assertFalse(restarted.owns_static)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])
        self.assertEqual(process.read(restarted.cave, len(expected_cave)), expected_cave)
        self.assertEqual(
            process.read(restarted.show_cave, len(expected_show_cave)),
            expected_show_cave,
        )

    def test_partial_roots_and_active_cave_mismatches_abort_read_only(self):
        cases = (
            "partial_tick",
            "partial_show",
            "partial_layout",
            "tick_cave",
            "show_cave",
            "state_magic",
        )
        for case in cases:
            with self.subTest(case=case):
                process = CountingGateProcess()
                owner = self.make_gate(process)
                owner.install()
                if case == "partial_tick":
                    process.seed(
                        gate_module.NATIVE_COMBAT_LOG_TICK_CALL_SITE,
                        gate_module.NATIVE_COMBAT_LOG_TICK_CALL_ORIGINAL,
                    )
                    error = "occupied/incomplete"
                elif case == "partial_show":
                    process.seed(
                        gate_module.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE,
                        gate_module.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_ORIGINAL,
                    )
                    error = "occupied/incomplete"
                elif case == "partial_layout":
                    site, original, _cave_offset = (
                        gate_module.NATIVE_COMBAT_LOG_LAYOUT_GATE_SITES[0]
                    )
                    process.seed(site, original)
                    error = "occupied/incomplete"
                elif case == "tick_cave":
                    process.seed(owner.cave, b"\xCC")
                    error = "tick cave mismatch"
                elif case == "show_cave":
                    process.seed(owner.show_cave, b"\xCC")
                    error = "show cave mismatch"
                else:
                    process.seed(owner.state + owner.MAGIC_OFFSET, b"BAD!")
                    error = "state mismatch"
                expected_memory = dict(process.memory)
                process.clear_activity()

                candidate = self.make_gate(process)
                with self.assertRaisesRegex(RuntimeError, error):
                    candidate.install()

                self.assertFalse(candidate.installed)
                self.assertFalse(candidate.owns_static)
                self.assertEqual(process.memory, expected_memory)
                self.assertEqual(process.writes, [])
                self.assertEqual(process.patches, [])

    def test_failure_at_each_mutation_never_rolls_back_published_code(self):
        show_site = gate_module.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE
        tick_site = gate_module.NATIVE_COMBAT_LOG_TICK_CALL_SITE
        for operation in range(1, 10):
            for after_mutation in (False, True):
                with self.subTest(
                    operation=operation,
                    after_mutation=after_mutation,
                ):
                    process = CountingGateProcess()
                    gate = self.make_gate(process)
                    process.arm_failure(
                        operation,
                        after_mutation=after_mutation,
                    )

                    with self.assertRaises(OSError):
                        gate.install()

                    recorded = operation if after_mutation else operation - 1
                    self.assertEqual(len(process.operations), recorded)
                    self.assertFalse(gate.installed)
                    self.assertFalse(gate.owns_static)
                    self.assertNotIn(
                        (
                            "patch",
                            show_site,
                            gate_module.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_ORIGINAL,
                        ),
                        process.operations,
                    )
                    self.assertNotIn(
                        (
                            "patch",
                            tick_site,
                            gate_module.NATIVE_COMBAT_LOG_TICK_CALL_ORIGINAL,
                        ),
                        process.operations,
                    )

                    show_committed = operation > 4 or (
                        operation == 4 and after_mutation
                    )
                    tick_committed = operation > 5 or (
                        operation == 5 and after_mutation
                    )
                    self.assertEqual(
                        process.read(show_site, 5),
                        gate._show_patch()
                        if show_committed
                        else gate_module.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_ORIGINAL,
                    )
                    self.assertEqual(
                        process.read(tick_site, 5),
                        gate._tick_patch()
                        if tick_committed
                        else gate_module.NATIVE_COMBAT_LOG_TICK_CALL_ORIGINAL,
                    )
                    for index, (
                        (layout_site, original, _cave_offset),
                        (_patch_site, patch),
                    ) in enumerate(
                        zip(
                            gate_module.NATIVE_COMBAT_LOG_LAYOUT_GATE_SITES,
                            gate._layout_patches(),
                        )
                    ):
                        layout_operation = 6 + index
                        committed = operation > layout_operation or (
                            operation == layout_operation and after_mutation
                        )
                        self.assertEqual(
                            process.read(layout_site, len(original)),
                            patch if committed else original,
                        )

                    if show_committed:
                        tick_size = (
                            gate_module.NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET
                            - gate_module.NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET
                        )
                        show_size = (
                            gate_module.NATIVE_COMBAT_LOG_GATE_STATE_OFFSET
                            - gate_module.NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET
                        )
                        self.assertEqual(
                            process.read(gate.cave, tick_size),
                            gate._build_tick_cave().ljust(tick_size, b"\x00"),
                        )
                        self.assertEqual(
                            process.read(gate.show_cave, show_size),
                            gate._build_show_cave().ljust(show_size, b"\x00"),
                        )

    def test_complete_prepared_image_resumes_without_rewriting_caves(self):
        process = CountingGateProcess()
        interrupted = self.make_gate(process)
        process.arm_failure(3, after_mutation=True)

        with self.assertRaises(OSError):
            interrupted.install()

        tick_size = (
            gate_module.NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET
            - gate_module.NATIVE_COMBAT_LOG_GATE_CAVE_OFFSET
        )
        show_size = (
            gate_module.NATIVE_COMBAT_LOG_GATE_STATE_OFFSET
            - gate_module.NATIVE_COMBAT_LOG_SHOW_GATE_CAVE_OFFSET
        )
        state_size = (
            gate_module.NATIVE_COMBAT_LOG_GATE_REGION_END
            - gate_module.NATIVE_COMBAT_LOG_GATE_STATE_OFFSET
        )
        expected_topology = (
            process.read(interrupted.cave, tick_size),
            process.read(interrupted.show_cave, show_size),
            process.read(interrupted.state, state_size),
        )
        process.clear_activity()
        resumed = self.make_gate(process)
        resumed.install()

        self.assertTrue(resumed.installed)
        self.assertTrue(resumed.owns_static)
        self.assertEqual(process.writes, [])
        self.assertEqual(
            [address for address, _payload in process.patches],
            [
                gate_module.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE,
                gate_module.NATIVE_COMBAT_LOG_TICK_CALL_SITE,
                *[
                    site
                    for site, _original, _cave_offset in (
                        gate_module.NATIVE_COMBAT_LOG_LAYOUT_GATE_SITES
                    )
                ],
            ],
        )
        self.assertEqual(
            (
                process.read(resumed.cave, tick_size),
                process.read(resumed.show_cave, show_size),
                process.read(resumed.state, state_size),
            ),
            expected_topology,
        )

    def test_partial_root_from_failed_commit_is_rejected_read_only(self):
        process = CountingGateProcess()
        interrupted = self.make_gate(process)
        process.arm_failure(5, after_mutation=False)

        with self.assertRaises(OSError):
            interrupted.install()

        expected_memory = dict(process.memory)
        process.clear_activity()
        candidate = self.make_gate(process)
        with self.assertRaisesRegex(RuntimeError, "occupied/incomplete"):
            candidate.install()

        self.assertEqual(process.memory, expected_memory)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_exception_after_final_root_is_hot_adopted_read_only(self):
        process = CountingGateProcess()
        interrupted = self.make_gate(process)
        process.arm_failure(9, after_mutation=True)

        with self.assertRaises(OSError):
            interrupted.install()

        expected_memory = dict(process.memory)
        process.clear_activity()
        adopted = self.make_gate(process)
        adopted.install()

        self.assertTrue(adopted.installed)
        self.assertFalse(adopted.owns_static)
        self.assertEqual(process.memory, expected_memory)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_inactive_unknown_cave_and_late_preflight_failure_do_not_mutate(self):
        for case in ("unknown_cave", "show_cave_read_failure"):
            with self.subTest(case=case):
                process = CountingGateProcess()
                gate = self.make_gate(process)
                if case == "unknown_cave":
                    process.seed(gate.cave, b"\xCC")
                    expected_error = RuntimeError
                else:
                    process.fail_reads.add(gate.show_cave)
                    expected_error = OSError
                expected_memory = dict(process.memory)
                process.clear_activity()

                with self.assertRaises(expected_error):
                    gate.install()

                self.assertEqual(process.memory, expected_memory)
                self.assertEqual(process.writes, [])
                self.assertEqual(process.patches, [])

    def test_restore_only_disarms_dynamic_state_and_keeps_resident_code(self):
        owned_process = CountingGateProcess()
        owner = self.make_gate(owned_process)
        owner.install()
        owner.request_hidden(True)
        owned_process.clear_activity()

        self.assertTrue(owner.restore(timeout=0))

        self.assertFalse(owner.installed)
        self.assertFalse(owner.owns_static)
        self.assertEqual(
            owned_process.read(gate_module.NATIVE_COMBAT_LOG_TICK_CALL_SITE, 5),
            owner._tick_patch(),
        )
        self.assertEqual(
            owned_process.read(
                gate_module.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE,
                5,
            ),
            owner._show_patch(),
        )
        self.assertEqual(owned_process.patches, [])
        self.assertEqual(
            owned_process.writes,
            [(owner.state + owner.DESIRED_OFFSET, b"\x00")],
        )

        resident_process = CountingGateProcess()
        resident_owner = self.make_gate(resident_process)
        resident_owner.install()
        resident_process.seed(
            resident_owner.state + resident_owner.DESIRED_OFFSET,
            b"\x01",
        )
        resident_process.seed(
            resident_owner.state + resident_owner.APPLIED_OFFSET,
            b"\x00",
        )
        adopted = self.make_gate(resident_process)
        resident_process.clear_activity()
        adopted.install()
        resident_process.clear_activity()

        self.assertTrue(adopted.restore(timeout=0))

        self.assertFalse(adopted.installed)
        self.assertFalse(adopted.owns_static)
        self.assertEqual(
            resident_process.read(
                gate_module.NATIVE_COMBAT_LOG_TICK_CALL_SITE,
                5,
            ),
            adopted._tick_patch(),
        )
        self.assertEqual(
            resident_process.read(
                gate_module.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE,
                5,
            ),
            adopted._show_patch(),
        )
        self.assertEqual(resident_process.patches, [])
        self.assertEqual(
            resident_process.writes,
            [(adopted.state + adopted.DESIRED_OFFSET, b"\x00")],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
