"""Byte-level cover for the generated polar-steering cave.

This code runs inside FF12 on the game thread, so it is verified by
disassembling the produced bytes rather than by trusting the generator.
"""

import unittest
import struct

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

import coop_runtime as movement
import movement_mailbox as mailbox


PROLOGUE = b"\x40\x53\x56\x41\x56"
COMMAND_BLOCK = 0x5BAA0200


def disassemble(code):
    md = Cs(CS_ARCH_X86, CS_MODE_64)
    return [
        (item.address, item.mnemonic, item.op_str) for item in md.disasm(bytes(code), 0)
    ]


class MovementCaveTests(unittest.TestCase):
    def setUp(self):
        movement.set_cave_base(0x5BAA0000)
        movement.set_dual_interaction_base(0x5C000000)
        self.cave = movement.build_movement_cave(COMMAND_BLOCK, PROLOGUE)
        self.listing = disassemble(self.cave)

    def test_shared_actor_physics_helper_follows_both_native_pointers(self):
        listing = disassemble(movement.build_actor_physics_helper())
        root_load = next(
            address
            for address, mnemonic, operand in listing
            if mnemonic == "mov" and operand == "rax, qword ptr [rax + 0x10]"
        )
        phys_load = next(
            address
            for address, mnemonic, operand in listing
            if mnemonic == "mov" and operand == "rax, qword ptr [rax + 0xb8]"
        )
        null_guards = [
            address
            for address, mnemonic, operand in listing
            if mnemonic == "test" and operand == "rax, rax"
        ]

        self.assertEqual(len(null_guards), 3)
        self.assertLess(null_guards[0], root_load)
        self.assertLess(root_load, null_guards[1])
        self.assertLess(null_guards[1], phys_load)
        self.assertLess(phys_load, null_guards[2])
        self.assertEqual(
            [mnemonic for _a, mnemonic, _o in listing[-4:]],
            ["clc", "ret", "stc", "ret"],
        )

    def test_p1_helper_compares_native_actor_without_following_mailbox_pointer(self):
        listing = disassemble(
            movement.build_p1_berserk_movement_helper(COMMAND_BLOCK)
        )
        state = movement.P1_BERSERK_MOVEMENT_STATE_OFFSET - 0x200
        actor_compare = next(
            address
            for address, mnemonic, operand in listing
            if mnemonic == "cmp"
            and operand
            == f"rax, qword ptr [r11 + {state + movement.P1_BERSERK_ACTOR_OFFSET:#x}]"
        )
        phys_load = next(
            address
            for address, mnemonic, operand in listing
            if mnemonic == "mov" and operand == "rax, qword ptr [rcx + 0xb8]"
        )

        self.assertLess(actor_compare, phys_load)
        self.assertNotIn(
            ("mov", "rax, qword ptr [rax + 0x10]"),
            [(mnemonic, operand) for _a, mnemonic, operand in listing],
        )
        self.assertIn(
            ("mov", "r10, qword ptr [rsp]"),
            [(mnemonic, operand) for _a, mnemonic, operand in listing],
        )

    def test_it_fits_the_reserved_region(self):
        # The jump back is appended by the installer; the final reserved bytes
        # contain the shared, null-safe actor -> root -> physics helper.
        self.assertLessEqual(
            len(self.cave) + 5,
            movement.ACTOR_PHYSICS_HELPER_OFFSET - movement.MOVEMENT_CAVE_OFFSET,
        )

    def test_every_byte_decodes(self):
        consumed = sum(
            len(bytes(self.cave)[address : address + 1])
            for address, _m, _o in self.listing
        )
        self.assertGreater(consumed, 0)
        last_address = self.listing[-1][0]
        self.assertLess(last_address, len(self.cave))

    def test_it_restores_every_scratch_register(self):
        mnemonics = [mnemonic for _a, mnemonic, _o in self.listing]
        self.assertEqual(mnemonics.count("push"), 6 + 3)  # scratch/actor + prologue
        self.assertEqual(mnemonics.count("pop"), 6)
        pushes = [op for _a, mnemonic, op in self.listing if mnemonic == "push"][:6]
        pops = [op for _a, mnemonic, op in self.listing if mnemonic == "pop"]
        self.assertEqual(pushes, ["rax", "r11", "r10", "r9", "rdx", "rax"])
        self.assertEqual(pops, ["rax", "rdx", "r9", "r10", "r11", "rax"])

    def test_it_ends_with_the_displaced_prologue(self):
        self.assertEqual(bytes(self.cave[-len(PROLOGUE) :]), PROLOGUE)

    def test_the_p2_path_reads_one_complete_seqlock_snapshot(self):
        base = movement.P2_MOVEMENT_MAILBOX_OFFSET - 0x200
        text = " | ".join(f"{m} {o}" for _a, m, o in self.listing)
        self.assertIn(f"mov r10d, dword ptr [r11 + {base:#x}]", text)
        self.assertIn(
            f"cmp dword ptr [r11 + {base + mailbox.ACTIVE_OFFSET:#x}], 0", text
        )
        self.assertIn("mov rax, qword ptr [rcx + 0x30]", text)
        self.assertIn(
            f"cmp rax, qword ptr [r11 + {base + mailbox.ACTOR_OFFSET:#x}]", text
        )
        self.assertNotIn(
            f"mov rax, qword ptr [r11 + {base + mailbox.ACTOR_OFFSET:#x}]", text
        )
        self.assertNotIn("mov rax, qword ptr [rax + 0x10]", text)
        self.assertIn("mov rax, qword ptr [rcx + 0xb8]", text)
        self.assertIn(
            f"cmp rax, qword ptr [r11 + {base + mailbox.PHYS_OFFSET:#x}]", text
        )
        self.assertIn(
            f"mov r9d, dword ptr [r11 + {base + mailbox.HEADING_OFFSET:#x}]", text
        )
        self.assertIn(
            f"mov edx, dword ptr [r11 + {base + mailbox.MAGNITUDE_OFFSET:#x}]", text
        )
        self.assertIn(f"cmp r10d, dword ptr [r11 + {base:#x}]", text)

    def test_each_role_rechecks_sequence_before_any_physics_store(self):
        stores = [
            address
            for address, mnemonic, operand in self.listing
            if mnemonic == "mov" and operand == "dword ptr [rax + 0xa4], r9d"
        ]
        self.assertEqual(len(stores), 2)
        for store, offset in zip(
            stores,
            (movement.P2_MOVEMENT_MAILBOX_OFFSET, movement.P3_MOVEMENT_MAILBOX_OFFSET),
        ):
            sequence = offset - 0x200 + mailbox.SEQUENCE_OFFSET
            final_read = max(
                address
                for address, mnemonic, operand in self.listing
                if mnemonic == "cmp"
                and operand == f"r10d, dword ptr [r11 + {sequence:#x}]"
                and address < store
            )
            self.assertLess(final_read, store)

    def test_the_p3_path_reads_only_its_atomic_mailbox(self):
        distance = movement.P3_MOVEMENT_MAILBOX_OFFSET - 0x200
        text = " | ".join(f"{m} {o}" for _a, m, o in self.listing)
        for field in (
            mailbox.SEQUENCE_OFFSET,
            mailbox.ACTIVE_OFFSET,
            mailbox.HEADING_OFFSET,
            mailbox.MAGNITUDE_OFFSET,
            mailbox.ACTOR_OFFSET,
            mailbox.PHYS_OFFSET,
        ):
            self.assertIn(f"[r11 + {distance + field:#x}]", text)

    def test_both_players_write_the_same_physics_fields(self):
        if movement.MOVEMENT_HOLDS_LEADER:
            self.skipTest(
                "diagnostic leader hold adds its own writes to the same fields"
            )
        writes = [
            op
            for _a, mnemonic, op in self.listing
            if mnemonic == "mov" and op.startswith("dword ptr [rax +")
        ]
        # heading, magnitude and pitch, once per player.
        self.assertEqual(len(writes), 6)
        for field in ("0xa4", "0xd0", "0xa0"):
            self.assertEqual(sum(1 for op in writes if f"[rax + {field}]" in op), 2)

    def test_native_action_approach_is_checked_for_both_players(self):
        if not movement.FOLLOWER_ACTION_NATIVE_APPROACH_RELEASE:
            self.skipTest("native action-approach handoff is disabled")
        calls = [
            (address, operand)
            for address, mnemonic, operand in self.listing
            if mnemonic == "call"
        ]
        self.assertEqual(len(calls), 2)
        self.assertLess(calls[0][0], calls[1][0])

    def test_it_is_gated_on_the_movement_flag(self):
        text = " | ".join(f"{m} {o}" for _a, m, o in self.listing)
        self.assertIn("cmp dword ptr [r11 + 0x38], 0", text)

    def test_native_auxiliary_vector_is_overridden_only_after_exact_match(self):
        r8_clears = [
            address
            for address, mnemonic, operand in self.listing
            if mnemonic == "xor" and operand == "r8d, r8d"
        ]
        stores = [
            address
            for address, mnemonic, operand in self.listing
            if mnemonic == "mov" and operand == "dword ptr [rax + 0xa0], r10d"
        ]

        self.assertEqual(len(r8_clears), 1)
        self.assertEqual(len(stores), 2)
        clear = r8_clears[0]
        for store in stores:
            self.assertLess(store, clear)

        r8_tests = [
            address
            for address, mnemonic, operand in self.listing
            if mnemonic == "test" and operand == "r8d, r8d"
        ]
        self.assertEqual(len(r8_tests), 1)
        self.assertGreater(r8_tests[0], max(stores))

        text = " | ".join(f"{m} {o}" for _a, m, o in self.listing)
        self.assertIn("test edx, 0x7fffffff", text)
        self.assertIn("bt qword ptr [r10], 0x2e", text)
        self.assertIn("mov rax, qword ptr [rsp + 0x10]", text)
        self.assertIn("cmp r10, qword ptr [rcx + 0x30]", text)
        self.assertIn("or r9d, dword ptr [rax + 4]", text)
        self.assertIn("or r9d, dword ptr [rax + 8]", text)
        self.assertIn("and r9d, 0x7fffffff", text)

        # A global R8D rejection was the intermittent P2/P3/P1 failure.  The
        # only remaining test is after exact stores and protects real native
        # root motion; the selector clear is downstream from all safety gates.
        safety_gates = [
            address
            for address, mnemonic, operand in self.listing
            if (mnemonic, operand)
            in {
                ("test", "edx, 0x7fffffff"),
                ("test", "r8d, r8d"),
                ("bt", "qword ptr [r10], 0x2e"),
                ("and", "r9d, 0x7fffffff"),
            }
        ]
        self.assertEqual(len(safety_gates), 4)
        self.assertTrue(
            all(address < clear for address in safety_gates),
        )

    def test_every_branch_lands_on_an_instruction_boundary(self):
        boundaries = {address for address, _m, _o in self.listing}
        boundaries.add(len(self.cave))
        for _address, mnemonic, op in self.listing:
            if mnemonic.startswith("j"):
                self.assertIn(
                    int(op, 16),
                    boundaries,
                    f"branch to {op} is not an instruction boundary",
                )

    def test_an_empty_p3_mailbox_is_rejected_before_actor_matching(self):
        base = movement.P3_MOVEMENT_MAILBOX_OFFSET - 0x200
        active_guard = next(
            address
            for address, mnemonic, operand in self.listing
            if mnemonic == "cmp"
            and operand == f"dword ptr [r11 + {base + mailbox.ACTIVE_OFFSET:#x}], 0"
        )
        native_actor_load = next(
            address
            for address, mnemonic, operand in self.listing
            if mnemonic == "mov"
            and operand == "rax, qword ptr [rsp]"
            and address > active_guard
        )
        actor_compare = next(
            address
            for address, mnemonic, operand in self.listing
            if mnemonic == "cmp"
            and operand
            == f"rax, qword ptr [r11 + {base + mailbox.ACTOR_OFFSET:#x}]"
            and address > native_actor_load
        )
        phys_load = next(
            address
            for address, mnemonic, operand in self.listing
            if mnemonic == "mov"
            and operand == "rax, qword ptr [rcx + 0xb8]"
            and address > actor_compare
        )
        self.assertLess(active_guard, native_actor_load)
        self.assertLess(native_actor_load, actor_compare)
        self.assertLess(actor_compare, phys_load)

    def test_poisoned_movement_actor_is_comparison_only_for_both_roles(self):
        poisoned_actor = 0xBFB1ECCC
        self.assertEqual(poisoned_actor + 0x10, 0xBFB1ECDC)
        text = " | ".join(f"{m} {o}" for _a, m, o in self.listing)

        self.assertNotIn("mov rax, qword ptr [rax + 0x10]", text)
        for mailbox_offset in (
            movement.P2_MOVEMENT_MAILBOX_OFFSET,
            movement.P3_MOVEMENT_MAILBOX_OFFSET,
        ):
            actor = mailbox_offset - 0x200 + mailbox.ACTOR_OFFSET
            self.assertIn(f"cmp rax, qword ptr [r11 + {actor:#x}]", text)
            self.assertNotIn(f"mov rax, qword ptr [r11 + {actor:#x}]", text)


class GeneratedActorIdentityGuardTests(unittest.TestCase):
    class FakeProcess:
        def __init__(self, site, original):
            self.site = site
            self.original = original
            self.writes = {}

        def read(self, address, size):
            if address in self.writes:
                return self.writes[address][:size]
            if address == self.site and size == len(self.original):
                return self.original
            return b"\x00" * size

        def write(self, address, data):
            self.writes[address] = bytes(data)

        def patch_code(self, address, data):
            if address != self.site:
                self.writes[address] = bytes(data)

    @staticmethod
    def _instruction_address(listing, mnemonic, operand):
        return next(
            address
            for address, item_mnemonic, item_operand in listing
            if item_mnemonic == mnemonic and item_operand == operand
        )

    @staticmethod
    def _internal_backward_jumps(code):
        backward = []
        for address, mnemonic, operand in disassemble(code):
            if not mnemonic.startswith("j"):
                continue
            try:
                target = int(operand, 0)
            except ValueError:
                continue
            if 0 <= target < len(code) and target <= address:
                backward.append((address, mnemonic, operand))
        return backward

    def setUp(self):
        movement.set_cave_base(0x5BAA0000)
        movement.set_dual_interaction_base(0x5C000000)

    def test_shared_actor_physics_helper_is_not_rewritten_once_published(self):
        class CountingProcess(self.FakeProcess):
            def __init__(self):
                super().__init__(movement.FACING_HOOK_SITE, b"\x00" * 8)
                self.write_count = 0

            def write(self, address, data):
                self.write_count += 1
                super().write(address, data)

        process = CountingProcess()
        self.assertTrue(movement.install_actor_physics_helper(process))
        self.assertFalse(movement.install_actor_physics_helper(process))
        self.assertEqual(process.write_count, 1)

    def test_movement_hook_rejects_an_unrecognized_jmp(self):
        process = self.FakeProcess(
            movement.MOVEMENT_HOOK_SITE,
            b"\xe9\x01\x02\x03\x04",
        )

        with self.assertRaisesRegex(RuntimeError, "unexpected bytes"):
            movement.install_movement_override(process)

        self.assertNotIn(movement.MOVEMENT_CAVE, process.writes)

    def test_facing_hook_rejects_a_foreign_jmp_target(self):
        process = self.FakeProcess(
            movement.FACING_HOOK_SITE,
            b"\xe9\x01\x02\x03\x04\x90\x90\x90",
        )

        with self.assertRaisesRegex(RuntimeError, "unexpected bytes"):
            movement.install_facing_override(process)

        self.assertNotIn(movement.FACING_CAVE, process.writes)

    def test_visual_facing_hook_rejects_a_foreign_call_target(self):
        process = self.FakeProcess(
            movement.VISUAL_FACING_CALL_SITE,
            b"\xe8\x01\x02\x03\x04",
        )

        with self.assertRaisesRegex(RuntimeError, "unexpected bytes"):
            movement.install_visual_facing_update(process)

        self.assertNotIn(movement.VISUAL_FACING_CAVE, process.writes)

    def test_movement_hook_rejects_the_exact_legacy_jmp_without_mutation(self):
        legacy_cave = movement.COMMAND_BLOCK - 0x200 + 0x100
        legacy_patch = b"\xe9" + movement.rel32(
            movement.MOVEMENT_HOOK_SITE,
            5,
            legacy_cave,
        )
        process = self.FakeProcess(movement.MOVEMENT_HOOK_SITE, legacy_patch)

        with self.assertRaisesRegex(RuntimeError, "legacy movement hook"):
            movement.install_movement_override(process)

        self.assertEqual(process.writes, {})

    def test_exact_live_movement_cave_is_adopted_without_executable_writes(self):
        patch = b"\xe9" + movement.rel32(
            movement.MOVEMENT_HOOK_SITE,
            5,
            movement.MOVEMENT_CAVE,
        )

        class RecordingProcess(self.FakeProcess):
            def __init__(self):
                super().__init__(movement.MOVEMENT_HOOK_SITE, patch)
                self.events = []

            def write(self, address, data):
                self.events.append(("write", address, bytes(data)))
                super().write(address, data)

            def patch_code(self, address, data):
                self.events.append(("patch", address, bytes(data)))

        process = RecordingProcess()
        cave = movement.build_movement_cave(COMMAND_BLOCK, PROLOGUE)
        jump_site = movement.MOVEMENT_CAVE + len(cave)
        cave += b"\xe9" + movement.rel32(
            jump_site,
            5,
            movement.MOVEMENT_HOOK_RETURN,
        )
        cave_size = (
            movement.ACTOR_PHYSICS_HELPER_OFFSET - movement.MOVEMENT_CAVE_OFFSET
        )
        process.writes[movement.MOVEMENT_CAVE] = cave.ljust(cave_size, b"\x00")

        self.assertFalse(movement.install_movement_override(process))
        self.assertEqual(process.events, [])

    def _seqlock_consumer_hooks(self):
        return (
            (
                "steering",
                movement.install_steering_heading_override,
                movement.STEERING_HEADING_STORE_SITE,
                bytes.fromhex("F3 0F 11 87 A4 00 00 00"),
                b"\xe9"
                + movement.rel32(
                    movement.STEERING_HEADING_STORE_SITE,
                    5,
                    movement.STEERING_HEADING_CAVE,
                )
                + b"\x90\x90\x90",
                movement.STEERING_HEADING_CAVE,
                (
                    movement.ACTOR_PHYSICS_HELPER,
                    movement.STEERING_FALLBACK_HELPER,
                ),
            ),
            (
                "facing",
                movement.install_facing_override,
                movement.FACING_HOOK_SITE,
                bytes.fromhex("F3 0F 11 83 5C 01 00 00"),
                b"\xe9"
                + movement.rel32(
                    movement.FACING_HOOK_SITE,
                    5,
                    movement.FACING_CAVE,
                )
                + b"\x90\x90\x90",
                movement.FACING_CAVE,
                (movement.ACTOR_PHYSICS_HELPER,),
            ),
            (
                "visual-facing",
                movement.install_visual_facing_update,
                movement.VISUAL_FACING_CALL_SITE,
                b"\xe8\x98\xd9\x02\x00",
                b"\xe8"
                + movement.rel32(
                    movement.VISUAL_FACING_CALL_SITE,
                    5,
                    movement.VISUAL_FACING_CAVE,
                ),
                movement.VISUAL_FACING_CAVE,
                (
                    movement.ACTOR_PHYSICS_HELPER,
                    movement.VISUAL_FALLBACK_HELPER,
                ),
            ),
        )

    class TransactionProcess:
        def __init__(
            self,
            site,
            root,
            *,
            fail_address=None,
            fail_patch_after=False,
        ):
            self.site = site
            self.site_bytes = bytes(root)
            self.fail_address = fail_address
            self.fail_patch_after = fail_patch_after
            self.regions = {}
            self.events = []

        def read(self, address, size):
            if address == self.site:
                return self.site_bytes[:size]
            if address in self.regions:
                return self.regions[address][:size].ljust(size, b"\x00")
            return b"\x00" * size

        def write(self, address, data):
            self.events.append(("write", address, bytes(data)))
            if self.fail_address == address:
                raise OSError("simulated cave write failure")
            self.regions[address] = bytes(data)

        def patch_code(self, address, data):
            self.events.append(("patch", address, bytes(data)))
            if address == self.site:
                self.site_bytes = bytes(data)
            if self.fail_patch_after:
                raise OSError("simulated post-commit failure")

    def test_seqlock_consumer_hooks_publish_caves_before_one_way_roots(self):
        for (
            name,
            installer,
            site,
            original,
            patch,
            cave,
            helpers,
        ) in self._seqlock_consumer_hooks():
            with self.subTest(name=name):
                process = self.TransactionProcess(site, original)

                self.assertTrue(installer(process))

                self.assertEqual(
                    [(kind, address) for kind, address, _data in process.events],
                    [
                        *(("write", helper) for helper in helpers),
                        ("write", cave),
                        ("patch", site),
                    ],
                )
                self.assertEqual(process.events[-1][2], patch)
                self.assertEqual(process.site_bytes, patch)
                self.assertNotIn(original, [event[2] for event in process.events])

    def test_exact_live_seqlock_consumer_topology_is_read_only_adopted(self):
        for (
            name,
            installer,
            site,
            original,
            patch,
            cave,
            helpers,
        ) in self._seqlock_consumer_hooks():
            with self.subTest(name=name):
                process = self.TransactionProcess(site, original)
                self.assertTrue(installer(process))
                process.events.clear()

                self.assertFalse(installer(process))

                self.assertEqual(process.events, [])
                self.assertEqual(process.site_bytes, patch)

    def test_active_seqlock_consumer_cave_mismatch_aborts_without_mutation(self):
        for (
            name,
            installer,
            site,
            original,
            patch,
            cave,
            helpers,
        ) in self._seqlock_consumer_hooks():
            with self.subTest(name=name):
                process = self.TransactionProcess(site, original)
                self.assertTrue(installer(process))
                image = bytearray(process.regions[cave])
                image[0] ^= 0xFF
                process.regions[cave] = bytes(image)
                process.events.clear()

                with self.assertRaisesRegex(RuntimeError, "active .* mismatch"):
                    installer(process)

                self.assertEqual(process.events, [])
                self.assertEqual(process.site_bytes, patch)

    def test_seqlock_consumer_write_failure_never_publishes_root(self):
        for (
            name,
            installer,
            site,
            original,
            _patch,
            cave,
            helpers,
        ) in self._seqlock_consumer_hooks():
            for fail_address in (*helpers, cave):
                with self.subTest(name=name, fail_address=hex(fail_address)):
                    process = self.TransactionProcess(
                        site,
                        original,
                        fail_address=fail_address,
                    )

                    with self.assertRaisesRegex(OSError, "simulated cave write failure"):
                        installer(process)

                    self.assertEqual(process.site_bytes, original)
                    self.assertFalse(
                        any(kind == "patch" for kind, _address, _data in process.events)
                    )

    def test_post_root_failure_leaves_an_exact_adoptable_topology(self):
        for (
            name,
            installer,
            site,
            original,
            patch,
            _cave,
            _helpers,
        ) in self._seqlock_consumer_hooks():
            with self.subTest(name=name):
                process = self.TransactionProcess(
                    site,
                    original,
                    fail_patch_after=True,
                )
                with self.assertRaisesRegex(OSError, "post-commit"):
                    installer(process)
                self.assertEqual(process.site_bytes, patch)

                process.fail_patch_after = False
                process.events.clear()
                self.assertFalse(installer(process))
                self.assertEqual(process.events, [])

    def test_visual_facing_validates_atomic_p2_and_p3_snapshots(self):
        process = self.FakeProcess(
            movement.VISUAL_FACING_CALL_SITE,
            b"\xe8\x98\xd9\x02\x00",
        )
        movement.install_visual_facing_update(process)
        listing = disassemble(process.writes[movement.VISUAL_FACING_CAVE])
        helper = disassemble(process.writes[movement.VISUAL_FALLBACK_HELPER])
        p1_berserk = (
            movement.P1_BERSERK_MOVEMENT_STATE_OFFSET
            - 0x200
            + movement.P1_BERSERK_ACTOR_OFFSET
        )
        for candidate, mailbox_offset in (
            ("P2", movement.P2_MOVEMENT_MAILBOX_OFFSET),
            ("P3", movement.P3_MOVEMENT_MAILBOX_OFFSET),
        ):
            with self.subTest(candidate=candidate):
                base = mailbox_offset - 0x200
                mailbox_select = self._instruction_address(
                    listing,
                    "lea",
                    f"r9, [r11 + {base:#x}]",
                )
                snapshot_write = self._instruction_address(
                    listing,
                    "mov",
                    f"dword ptr [r11 + {base + mailbox.NATIVE_SELECTED_VISUAL_OFFSET:#x}], r10d",
                )
                snapshot_pointer = self._instruction_address(
                    listing,
                    "lea",
                    f"r10, [r11 + {base + mailbox.NATIVE_SELECTED_VISUAL_OFFSET:#x}]",
                )
                call = next(
                    address
                    for address, mnemonic, _operand in listing
                    if mnemonic == "call" and mailbox_select < address < snapshot_write
                )
                carry_guard = next(
                    address
                    for address, mnemonic, _operand in listing
                    if mnemonic == "jb" and call < address < snapshot_write
                )
                self.assertLess(mailbox_select, call)
                self.assertLess(call, carry_guard)
                self.assertLess(carry_guard, snapshot_write)
                self.assertLess(snapshot_write, snapshot_pointer)

        current_actor = self._instruction_address(
            helper, "cmp", "rax, qword ptr [r9 + 8]"
        )
        current_phys = self._instruction_address(
            helper, "cmp", f"r10, qword ptr [r9 + {mailbox.PHYS_OFFSET:#x}]"
        )
        fallback_actor = self._instruction_address(
            helper,
            "cmp",
            f"rax, qword ptr [r9 + {mailbox.FALLBACK_ACTOR_OFFSET:#x}]",
        )
        fallback_phys = self._instruction_address(
            helper,
            "cmp",
            f"r10, qword ptr [r9 + {mailbox.FALLBACK_PHYS_OFFSET:#x}]",
        )
        current_resolve_call = next(
            address
            for address, mnemonic, _operand in helper
            if mnemonic == "call" and current_actor < address < current_phys
        )
        fallback_resolve_call = next(
            address
            for address, mnemonic, _operand in helper
            if mnemonic == "call" and fallback_actor < address < fallback_phys
        )
        self.assertLess(current_actor, current_resolve_call)
        self.assertLess(current_resolve_call, current_phys)
        self.assertLess(fallback_actor, fallback_resolve_call)
        self.assertLess(fallback_resolve_call, fallback_phys)

        fallback_read = self._instruction_address(
            helper,
            "mov",
            f"edx, dword ptr [r9 + {mailbox.FALLBACK_SEQUENCE_OFFSET:#x}]",
        )
        main_final = self._instruction_address(helper, "cmp", "edx, dword ptr [r9]")
        fallback_final = self._instruction_address(
            helper,
            "cmp",
            f"edx, dword ptr [r9 + {mailbox.FALLBACK_SEQUENCE_OFFSET:#x}]",
        )
        main_odd = next(
            index
            for index, (_address, mnemonic, operand) in enumerate(helper)
            if mnemonic == "test" and operand == "dl, 1"
        )
        fallback_odd = next(
            index
            for index, (_address, mnemonic, operand) in enumerate(helper)
            if mnemonic == "test" and operand == "dl, 1" and index > main_odd
        )
        miss = self._instruction_address(helper, "stc", "")
        for source in (
            main_odd,
            next(i for i, item in enumerate(helper) if item[0] == main_final),
        ):
            self.assertEqual(int(helper[source + 1][2], 16), fallback_read)
        for source in (
            fallback_odd,
            next(i for i, item in enumerate(helper) if item[0] == fallback_final),
        ):
            self.assertEqual(int(helper[source + 1][2], 16), miss)
        zero_guard = self._instruction_address(
            listing, "cmp", f"qword ptr [r11 + {p1_berserk:#x}], 0"
        )
        pointer_compare = self._instruction_address(
            listing, "cmp", f"rax, qword ptr [r11 + {p1_berserk:#x}]"
        )
        self.assertLess(zero_guard, pointer_compare)

    def test_visual_facing_accepts_solo_p1_berserk_without_follower_master(self):
        process = self.FakeProcess(
            movement.VISUAL_FACING_CALL_SITE,
            b"\xe8\x98\xd9\x02\x00",
        )
        movement.install_visual_facing_update(process)
        listing = disassemble(process.writes[movement.VISUAL_FACING_CAVE])
        p1_active = (
            movement.P1_BERSERK_MOVEMENT_STATE_OFFSET
            - 0x200
            + movement.P1_BERSERK_ACTIVE_OFFSET
        )
        p1_gate = next(
            index
            for index, (_address, mnemonic, operand) in enumerate(listing)
            if mnemonic == "cmp" and operand == f"dword ptr [r11 + {p1_active:#x}], 0"
        )
        follower_gate = next(
            index
            for index, (_address, mnemonic, operand) in enumerate(listing)
            if mnemonic == "cmp" and operand == "dword ptr [r11 + 0x38], 0"
        )
        self.assertLess(p1_gate, follower_gate)
        self.assertEqual(listing[p1_gate + 1][1], "jne")
        self.assertEqual(listing[follower_gate + 1][1], "je")

    def test_steering_heading_rechecks_private_p2_snapshot_before_commit(self):
        process = self.FakeProcess(
            movement.STEERING_HEADING_STORE_SITE,
            bytes.fromhex("F3 0F 11 87 A4 00 00 00"),
        )
        movement.install_steering_heading_override(process)
        listing = disassemble(process.writes[movement.STEERING_HEADING_CAVE])
        helper = disassemble(process.writes[movement.STEERING_FALLBACK_HELPER])
        base = movement.P2_MOVEMENT_MAILBOX_OFFSET - 0x200
        mailbox_select = self._instruction_address(
            listing, "lea", f"r9, [r11 + {base:#x}]"
        )
        published_phys_compare = self._instruction_address(
            listing,
            "cmp",
            f"rdi, qword ptr [r9 + {mailbox.PHYS_OFFSET:#x}]",
        )
        heading_read = self._instruction_address(
            listing,
            "mov",
            f"eax, dword ptr [r9 + {mailbox.HEADING_OFFSET:#x}]",
        )
        sequence_recheck = self._instruction_address(
            listing, "cmp", "r10d, dword ptr [r9]"
        )
        self.assertLess(mailbox_select, published_phys_compare)
        self.assertLess(published_phys_compare, heading_read)
        self.assertLess(heading_read, sequence_recheck)

        fallback_phys = self._instruction_address(
            helper,
            "cmp",
            f"rdi, qword ptr [r9 + {mailbox.FALLBACK_PHYS_OFFSET:#x}]",
        )
        fallback_heading = self._instruction_address(
            helper,
            "mov",
            f"eax, dword ptr [r9 + {mailbox.FALLBACK_HEADING_OFFSET:#x}]",
        )
        fallback_final = self._instruction_address(
            helper,
            "cmp",
            f"r10d, dword ptr [r9 + {mailbox.FALLBACK_SEQUENCE_OFFSET:#x}]",
        )
        self.assertLess(fallback_phys, fallback_heading)
        self.assertLess(fallback_heading, fallback_final)

        # A poisoned mailbox actor with the correct native physics pointer must
        # remain inert data.  Steering identifies the live object by RDI/phys
        # and neither cave loads nor dereferences either actor field.
        poisoned_actor = 0xBFB1ECCC
        self.assertNotEqual(poisoned_actor, 0)
        actor_operands = (
            "qword ptr [r9 + 8]",
            f"qword ptr [r9 + {mailbox.FALLBACK_ACTOR_OFFSET:#x}]",
        )
        for region in (listing, helper):
            self.assertFalse(
                [
                    (address, mnemonic, operand)
                    for address, mnemonic, operand in region
                    if any(marker in operand for marker in actor_operands)
                ]
            )
        self.assertFalse(
            [item for item in helper if item[1] == "call"],
            "steering fallback must not call through a mailbox actor",
        )

        p2_main_read = self._instruction_address(
            listing, "mov", "r10d, dword ptr [r9]"
        )
        p3_base = movement.P3_MOVEMENT_MAILBOX_OFFSET - 0x200
        p3_select = self._instruction_address(
            listing, "lea", f"r9, [r11 + {p3_base:#x}]"
        )
        p2_call_index = next(
            index
            for index, (address, mnemonic, _operand) in enumerate(listing)
            if mnemonic == "call" and sequence_recheck < address
        )
        self.assertEqual(listing[p2_call_index + 1][1], "jb")
        self.assertEqual(listing[p2_call_index + 2][1], "jne")
        self.assertEqual(int(listing[p2_call_index + 2][2], 16), p3_select)
        self.assertGreater(p3_select, p2_main_read)
        main_odd_index = next(
            index
            for index, (_address, mnemonic, operand) in enumerate(listing)
            if mnemonic == "test" and operand == "r10b, 1"
        )
        main_final_index = next(
            index
            for index, (address, _mnemonic, _operand) in enumerate(listing)
            if address == sequence_recheck
        )
        fallback_call = listing[p2_call_index][0]
        self.assertEqual(int(listing[main_odd_index + 1][2], 16), fallback_call)
        self.assertEqual(int(listing[main_final_index + 1][2], 16), fallback_call)

    def test_actor_facing_validates_atomic_p2_and_p3_snapshots(self):
        process = self.FakeProcess(
            movement.FACING_HOOK_SITE,
            bytes.fromhex("F3 0F 11 83 5C 01 00 00"),
        )
        movement.install_facing_override(process)
        listing = disassemble(process.writes[movement.FACING_CAVE])
        p1_berserk = (
            movement.P1_BERSERK_MOVEMENT_STATE_OFFSET
            - 0x200
            + movement.P1_BERSERK_ACTOR_OFFSET
        )
        for mailbox_offset in (
            movement.P2_MOVEMENT_MAILBOX_OFFSET,
            movement.P3_MOVEMENT_MAILBOX_OFFSET,
        ):
            base = mailbox_offset - 0x200
            actor_compare = self._instruction_address(
                listing,
                "cmp",
                f"rbx, qword ptr [r11 + {base + mailbox.ACTOR_OFFSET:#x}]",
            )
            phys_compare = self._instruction_address(
                listing,
                "cmp",
                f"rax, qword ptr [r11 + {base + mailbox.PHYS_OFFSET:#x}]",
            )
            resolve_call = next(
                address
                for address, mnemonic, _operand in listing
                if mnemonic == "call" and actor_compare < address < phys_compare
            )
            identity_guard = next(
                address
                for address, mnemonic, _operand in listing
                if mnemonic == "jb" and resolve_call < address < phys_compare
            )
            visual_read = self._instruction_address(
                listing,
                "mov",
                f"r9d, dword ptr [r11 + {base + mailbox.VISUAL_HEADING_OFFSET:#x}]",
            )
            sequence_recheck = self._instruction_address(
                listing, "cmp", f"r10d, dword ptr [r11 + {base:#x}]"
            )
            main_read = self._instruction_address(
                listing, "mov", f"r10d, dword ptr [r11 + {base:#x}]"
            )
            fallback_sequence = base + mailbox.FALLBACK_SEQUENCE_OFFSET
            fallback_read = self._instruction_address(
                listing,
                "mov",
                f"r10d, dword ptr [r11 + {fallback_sequence:#x}]",
            )
            fallback_actor = self._instruction_address(
                listing,
                "cmp",
                f"rbx, qword ptr [r11 + {base + mailbox.FALLBACK_ACTOR_OFFSET:#x}]",
            )
            fallback_phys = self._instruction_address(
                listing,
                "cmp",
                f"rax, qword ptr [r11 + {base + mailbox.FALLBACK_PHYS_OFFSET:#x}]",
            )
            fallback_resolve_call = next(
                address
                for address, mnemonic, _operand in listing
                if mnemonic == "call"
                and fallback_actor < address < fallback_phys
            )
            fallback_identity_guard = next(
                address
                for address, mnemonic, _operand in listing
                if mnemonic == "jb"
                and fallback_resolve_call < address < fallback_phys
            )
            fallback_visual = self._instruction_address(
                listing,
                "mov",
                f"r9d, dword ptr [r11 + {base + mailbox.FALLBACK_VISUAL_HEADING_OFFSET:#x}]",
            )
            fallback_recheck = self._instruction_address(
                listing,
                "cmp",
                f"r10d, dword ptr [r11 + {fallback_sequence:#x}]",
            )
            self.assertLess(actor_compare, resolve_call)
            self.assertLess(resolve_call, identity_guard)
            self.assertLess(identity_guard, phys_compare)
            self.assertLess(phys_compare, visual_read)
            self.assertLess(visual_read, sequence_recheck)
            self.assertLess(fallback_actor, fallback_resolve_call)
            self.assertLess(fallback_resolve_call, fallback_identity_guard)
            self.assertLess(fallback_identity_guard, fallback_phys)
            self.assertLess(fallback_phys, fallback_visual)
            self.assertLess(fallback_visual, fallback_recheck)

            main_odd = next(
                index
                for index, (address, mnemonic, operand) in enumerate(listing)
                if address > main_read
                and address < fallback_read
                and mnemonic == "test"
                and operand == "r10b, 1"
            )
            fallback_odd = next(
                index
                for index, (address, mnemonic, operand) in enumerate(listing)
                if address > fallback_read
                and address < fallback_recheck
                and mnemonic == "test"
                and operand == "r10b, 1"
            )
            main_final = next(
                index
                for index, (address, _mnemonic, _operand) in enumerate(listing)
                if address == sequence_recheck
            )
            fallback_final = next(
                index
                for index, (address, _mnemonic, _operand) in enumerate(listing)
                if address == fallback_recheck
            )
            self.assertEqual(int(listing[main_odd + 1][2], 16), fallback_read)
            self.assertEqual(int(listing[main_final + 1][2], 16), fallback_read)
            fallback_odd_target = int(listing[fallback_odd + 1][2], 16)
            fallback_changed_target = int(listing[fallback_final + 1][2], 16)
            self.assertEqual(fallback_odd_target, fallback_changed_target)
            self.assertGreater(fallback_odd_target, fallback_recheck)
            self.assertNotEqual(fallback_odd_target, main_read)
        zero_guard = self._instruction_address(
            listing, "cmp", f"qword ptr [r11 + {p1_berserk:#x}], 0"
        )
        pointer_compare = self._instruction_address(
            listing, "cmp", f"rbx, qword ptr [r11 + {p1_berserk:#x}]"
        )
        self.assertLess(zero_guard, pointer_compare)

    def test_visual_facing_preserves_actor_when_heading_bits_look_like_a_pointer(self):
        process = self.FakeProcess(
            movement.VISUAL_FACING_CALL_SITE,
            b"\xe8\x98\xd9\x02\x00",
        )
        movement.install_visual_facing_update(process)
        listing = disassemble(process.writes[movement.VISUAL_FACING_CAVE])
        helper = disassemble(process.writes[movement.VISUAL_FALLBACK_HELPER])

        # 0xBFB1ECCC was the exact poisoned heading from the crash dump.  Its
        # numeric value is irrelevant now: heading travels in R10D and is copied
        # straight to scalar scratch, never through the actor register EAX/RAX.
        poisoned_heading = 0xBFB1ECCC
        self.assertEqual(poisoned_heading + 0x6B4, 0xBFB1F380)
        self.assertNotIn(
            ("mov", "eax, r10d"),
            [(mnemonic, operand) for _address, mnemonic, operand in helper],
        )
        helper_calls = [
            index
            for index, (_address, mnemonic, _operand) in enumerate(helper)
            if mnemonic == "call"
        ]
        self.assertEqual(len(helper_calls), 2)
        for call in helper_calls:
            self.assertEqual(helper[call - 1][1:], ("push", "rax"))
            self.assertEqual(helper[call + 1][1:], ("mov", "r10, rax"))
            self.assertEqual(helper[call + 2][1:], ("pop", "rax"))
        for mailbox_offset in (
            movement.P2_MOVEMENT_MAILBOX_OFFSET,
            movement.P3_MOVEMENT_MAILBOX_OFFSET,
        ):
            base = mailbox_offset - 0x200
            self._instruction_address(
                listing,
                "mov",
                f"dword ptr [r11 + {base + mailbox.NATIVE_SELECTED_VISUAL_OFFSET:#x}], r10d",
            )

        # With idle cancellation disabled, the generated cave contains none of
        # the dead actor probes that consumed the poisoned RAX at cave + B1.
        self.assertFalse(movement.ENABLE_IDLE_MOTION_CANCEL)
        actor_dereferences = [
            (address, mnemonic, operand)
            for address, mnemonic, operand in listing
            if "[rax" in operand
            and any(
                marker in operand
                for marker in ("+ 0x6b4]", "+ 0x714]", "dword ptr [rax]")
            )
        ]
        self.assertEqual(actor_dereferences, [])

        native_physics_reload = self._instruction_address(
            listing, "mov", "rax, qword ptr [rcx + 0xb8]"
        )
        p2_base = movement.P2_MOVEMENT_MAILBOX_OFFSET - 0x200
        p2_scratch = self._instruction_address(
            listing,
            "mov",
            f"dword ptr [r11 + {p2_base + mailbox.NATIVE_SELECTED_VISUAL_OFFSET:#x}], r10d",
        )
        self.assertLess(p2_scratch, native_physics_reload)
        self.assertFalse(
            [
                (address, mnemonic, operand)
                for address, mnemonic, operand in listing
                if p2_scratch < address < native_physics_reload
                and mnemonic == "mov"
                and operand.startswith(("rax,", "eax,"))
            ]
        )

    def test_visual_facing_mirrors_the_native_call_parameter_guards(self):
        process = self.FakeProcess(
            movement.VISUAL_FACING_CALL_SITE,
            b"\xe8\x98\xd9\x02\x00",
        )
        movement.install_visual_facing_update(process)
        listing = disassemble(process.writes[movement.VISUAL_FACING_CAVE])

        original_tail = listing[-1][0]
        rcx_test = next(
            index
            for index, (_address, mnemonic, operand) in enumerate(listing)
            if mnemonic == "test" and operand == "rcx, rcx"
        )
        type_load = self._instruction_address(
            listing, "movzx", "edx, byte ptr [rcx + 3]"
        )
        type_compare = self._instruction_address(listing, "cmp", "edx, 2")
        actor_load = self._instruction_address(
            listing, "mov", "rax, qword ptr [rcx + 0x30]"
        )
        actor_test = next(
            index
            for index, (address, mnemonic, operand) in enumerate(listing)
            if address > actor_load and mnemonic == "test" and operand == "rax, rax"
        )

        self.assertEqual(listing[rcx_test + 1][1], "je")
        self.assertEqual(int(listing[rcx_test + 1][2], 16), original_tail)
        self.assertLess(type_load, type_compare)
        type_compare_index = next(
            index for index, item in enumerate(listing) if item[0] == type_compare
        )
        self.assertEqual(listing[type_compare_index + 1][1], "jne")
        self.assertEqual(int(listing[type_compare_index + 1][2], 16), original_tail)
        self.assertLess(type_compare, actor_load)
        self.assertEqual(listing[actor_test + 1][1], "je")
        self.assertEqual(int(listing[actor_test + 1][2], 16), original_tail)

    def test_stuck_or_mutating_seqlocks_cannot_form_a_native_retry_cycle(self):
        cases = (
            (
                "steering",
                movement.install_steering_heading_override,
                movement.STEERING_HEADING_STORE_SITE,
                bytes.fromhex("F3 0F 11 87 A4 00 00 00"),
                (
                    movement.STEERING_HEADING_CAVE,
                    movement.STEERING_FALLBACK_HELPER,
                    movement.ACTOR_PHYSICS_HELPER,
                ),
            ),
            (
                "facing",
                movement.install_facing_override,
                movement.FACING_HOOK_SITE,
                bytes.fromhex("F3 0F 11 83 5C 01 00 00"),
                (movement.FACING_CAVE, movement.ACTOR_PHYSICS_HELPER),
            ),
            (
                "visual-facing",
                movement.install_visual_facing_update,
                movement.VISUAL_FACING_CALL_SITE,
                b"\xe8\x98\xd9\x02\x00",
                (
                    movement.VISUAL_FACING_CAVE,
                    movement.VISUAL_FALLBACK_HELPER,
                    movement.ACTOR_PHYSICS_HELPER,
                ),
            ),
        )

        # Main can make one forward transition to fallback.  Fallback-odd and
        # fallback-changed both take a forward fail-open edge.  With no internal
        # back edge in any reachable native region, permanently odd or mutating
        # publisher words cannot keep the game thread in a retry cycle.
        for name, installer, site, original, regions in cases:
            with self.subTest(consumer=name):
                process = self.FakeProcess(site, original)
                installer(process)
                for region in regions:
                    code = process.writes[region]
                    self.assertEqual(
                        self._internal_backward_jumps(code),
                        [],
                        f"{name} has a native back edge in {region:#x}",
                    )

    def test_actor_facing_accepts_solo_p1_berserk_without_follower_master(self):
        process = self.FakeProcess(
            movement.FACING_HOOK_SITE,
            bytes.fromhex("F3 0F 11 83 5C 01 00 00"),
        )
        movement.install_facing_override(process)
        listing = disassemble(process.writes[movement.FACING_CAVE])
        p1_active = (
            movement.P1_BERSERK_MOVEMENT_STATE_OFFSET
            - 0x200
            + movement.P1_BERSERK_ACTIVE_OFFSET
        )
        p1_gate = next(
            index
            for index, (_address, mnemonic, operand) in enumerate(listing)
            if mnemonic == "cmp" and operand == f"dword ptr [r11 + {p1_active:#x}], 0"
        )
        follower_gate = next(
            index
            for index, (_address, mnemonic, operand) in enumerate(listing)
            if mnemonic == "cmp" and operand == "dword ptr [r11 + 0x38], 0"
        )
        self.assertLess(p1_gate, follower_gate)
        self.assertEqual(listing[p1_gate + 1][1], "jne")
        self.assertEqual(listing[follower_gate + 1][1], "je")

    def test_follower_list_rejects_null_p2_and_p3_before_matching(self):
        process = self.FakeProcess(
            movement.FOLLOWER_LIST_EVALUATOR_SITE,
            bytes.fromhex("48 89 9C 24 80 00 00 00"),
        )
        movement.install_follower_list_guard(process)
        listing = disassemble(process.writes[movement.FOLLOWER_LIST_GUARD_CAVE])
        p3 = movement.P3_COMMAND_BLOCK_OFFSET - 0x200 + movement.P3_ACTOR_OFFSET
        for zero_operand, match_operand in (
            (
                "qword ptr [r11 + 0x10], 0",
                "r13, qword ptr [r11 + 0x10]",
            ),
            (
                f"qword ptr [r11 + {p3:#x}], 0",
                f"r13, qword ptr [r11 + {p3:#x}]",
            ),
        ):
            zero_guard = self._instruction_address(listing, "cmp", zero_operand)
            pointer_compare = self._instruction_address(listing, "cmp", match_operand)
            self.assertLess(zero_guard, pointer_compare)


class SecondPlayerPublisherTests(unittest.TestCase):
    """P2 publishes its mailbox without owning the shared P2/P3 master."""

    class FakeProcess:
        def __init__(self, actor=0):
            self.memory = {movement.COMMAND_BLOCK + 0x10: struct.pack("<Q", actor)}
            self.writes = []

        def u64(self, address):
            return struct.unpack("<Q", self.memory[address])[0]

        def write(self, address, data):
            data = bytes(data)
            self.writes.append((address, data))
            self.memory[address] = data

    def test_actor_swap_hides_p2_until_new_command_is_coherent(self):
        process = self.FakeProcess(actor=0x1111)
        movement.write_command(
            process,
            0x2222,
            1.0,
            1.1,
            0.12,
            True,
            phys=0x3333,
        )
        actor_writes = [
            data
            for address, data in process.writes
            if address == movement.COMMAND_BLOCK + 0x10
        ]
        self.assertEqual(
            actor_writes,
            [struct.pack("<Q", 0), struct.pack("<Q", 0x2222)],
        )
        phys_writes = [
            data
            for address, data in process.writes
            if address == movement.COMMAND_BLOCK + 0x78
        ]
        self.assertEqual(
            phys_writes,
            [struct.pack("<Q", 0), struct.pack("<Q", 0x3333)],
        )
        self.assertNotIn(
            movement.COMMAND_BLOCK + movement.MOVEMENT_ACTIVE_OFFSET,
            [address for address, _data in process.writes],
        )

    def test_release_does_not_drop_a_master_that_p3_may_still_own(self):
        process = self.FakeProcess(actor=0x1111)
        movement.write_command(process, 0, 1.0, 1.1, 0.0, False)
        self.assertNotIn(
            movement.COMMAND_BLOCK + movement.MOVEMENT_ACTIVE_OFFSET,
            [address for address, _data in process.writes],
        )
        self.assertEqual(
            process.memory[movement.COMMAND_BLOCK + 0x10],
            struct.pack("<Q", 0),
        )
        self.assertEqual(
            process.memory[movement.COMMAND_BLOCK + 0x78],
            struct.pack("<Q", 0),
        )

    def test_shared_master_tracks_the_aggregate_follower_state(self):
        process = self.FakeProcess(actor=0)
        movement.publish_follower_movement_master(process, True)
        movement.publish_follower_movement_master(process, False)
        self.assertEqual(
            process.writes,
            [
                (
                    movement.COMMAND_BLOCK + movement.MOVEMENT_ACTIVE_OFFSET,
                    struct.pack("<I", 1),
                ),
                (
                    movement.COMMAND_BLOCK + movement.MOVEMENT_ACTIVE_OFFSET,
                    struct.pack("<I", 0),
                ),
            ],
        )

    def test_p2_ko_cannot_drop_the_master_while_p3_survives(self):
        process = self.FakeProcess(actor=0x1111)
        movement.publish_follower_movement_master(process, True)
        first_write_count = len(process.writes)
        movement.write_command(process, 0, 1.0, 1.1, 0.0, False)
        movement.publish_follower_movement_master(process, True)
        master_writes = [
            data
            for address, data in process.writes[first_write_count:]
            if address == movement.COMMAND_BLOCK + movement.MOVEMENT_ACTIVE_OFFSET
        ]
        self.assertEqual(master_writes, [struct.pack("<I", 1)])

    def test_p3_only_master_never_publishes_a_p2_phys_without_actor(self):
        process = self.FakeProcess(actor=0)
        movement.write_command(
            process,
            0,
            1.0,
            1.1,
            0.0,
            True,
            phys=0xDEADBEEF,
        )
        self.assertEqual(
            process.memory[movement.COMMAND_BLOCK + 0x10],
            struct.pack("<Q", 0),
        )
        self.assertEqual(
            process.memory[movement.COMMAND_BLOCK + 0x78],
            struct.pack("<Q", 0),
        )

    def test_role_loss_hides_actor_then_phys_before_late_publisher(self):
        process = self.FakeProcess(actor=0x1111)
        movement.write_command(process, 0x1111, 1.0, 1.1, 0.12, True, phys=0x3333)
        process.writes.clear()
        movement.guard_p2_publication_generation(
            process,
            0x1111,
            False,
        )
        self.assertEqual(
            [address for address, _data in process.writes[:6]],
            [
                movement.P2_MOVEMENT_MAILBOX + mailbox.FALLBACK_SEQUENCE_OFFSET,
                movement.P2_MOVEMENT_MAILBOX + mailbox.FALLBACK_ACTIVE_OFFSET,
                movement.P2_MOVEMENT_MAILBOX + mailbox.FALLBACK_SEQUENCE_OFFSET,
                movement.P2_MOVEMENT_MAILBOX,
                movement.P2_MOVEMENT_MAILBOX + mailbox.ACTIVE_OFFSET,
                movement.P2_MOVEMENT_MAILBOX,
            ],
        )
        self.assertTrue(struct.unpack("<I", process.writes[0][1])[0] & 1)
        self.assertFalse(struct.unpack("<I", process.writes[2][1])[0] & 1)
        self.assertTrue(struct.unpack("<I", process.writes[3][1])[0] & 1)
        self.assertFalse(struct.unpack("<I", process.writes[5][1])[0] & 1)
        released = mailbox.MovementCommand.from_payload(process.writes[4][1])
        self.assertFalse(released.active)
        self.assertEqual(
            process.writes[6:8],
            [
                (
                    movement.COMMAND_BLOCK + 0x10,
                    struct.pack("<Q", 0),
                ),
                (
                    movement.COMMAND_BLOCK + 0x78,
                    struct.pack("<Q", 0),
                ),
            ],
        )


class ThirdPlayerPublisherTests(unittest.TestCase):
    """Ordering is the contract: the cave reads this while the game runs."""

    class FakeProcess:
        def __init__(self):
            self.writes = []

        def write(self, address, data):
            self.writes.append((address, data))

    def setUp(self):
        movement.COMMAND_BLOCK = 0x5BAA0200
        self.block = movement.third_player_block()
        self.process = self.FakeProcess()

    def test_the_block_is_the_reserved_one(self):
        self.assertEqual(
            self.block - (movement.COMMAND_BLOCK - 0x200),
            movement.P3_COMMAND_BLOCK_OFFSET,
        )

    def test_ownership_is_claimed_only_after_the_command_is_visible(self):
        movement.write_third_player_command(
            self.process, 0x1234, 1.5, 0.12, True, phys=0x5678
        )
        self.assertEqual(
            [address for address, _data in self.process.writes[:6]],
            [
                movement.P3_MOVEMENT_MAILBOX + mailbox.FALLBACK_SEQUENCE_OFFSET,
                movement.P3_MOVEMENT_MAILBOX + mailbox.FALLBACK_ACTIVE_OFFSET,
                movement.P3_MOVEMENT_MAILBOX + mailbox.FALLBACK_SEQUENCE_OFFSET,
                movement.P3_MOVEMENT_MAILBOX,
                movement.P3_MOVEMENT_MAILBOX + mailbox.ACTIVE_OFFSET,
                movement.P3_MOVEMENT_MAILBOX,
            ],
        )
        published = mailbox.MovementCommand.from_payload(self.process.writes[4][1])
        self.assertTrue(published.active)
        self.assertEqual((published.actor, published.phys), (0x1234, 0x5678))
        offsets = [address - self.block for address, _data in self.process.writes[6:]]
        self.assertEqual(offsets[-1], movement.P3_MOVEMENT_ACTIVE_OFFSET)
        self.assertIn(movement.P3_ACTOR_OFFSET, offsets[:-1])
        self.assertIn(movement.P3_HEADING_OFFSET, offsets[:-1])
        self.assertIn(movement.P3_MAGNITUDE_OFFSET, offsets[:-1])
        self.assertIn(movement.P3_PHYS_OFFSET, offsets[:-1])
        self.assertLess(
            len(offsets) - 1 - offsets[::-1].index(movement.P3_PHYS_OFFSET),
            len(offsets) - 1 - offsets[::-1].index(movement.P3_ACTOR_OFFSET),
        )

    def test_actor_swap_drops_private_ownership_before_republishing(self):
        movement.write_third_player_command(
            self.process, 0x1111, 1.0, 0.1, True, phys=0x4444
        )
        self.process.writes.clear()
        movement.write_third_player_command(
            self.process, 0x2222, 1.5, 0.12, True, phys=0x3333
        )
        offsets = [address - self.block for address, _data in self.process.writes[6:]]
        self.assertEqual(offsets[0], movement.P3_MOVEMENT_ACTIVE_OFFSET)
        self.assertEqual(offsets[-1], movement.P3_MOVEMENT_ACTIVE_OFFSET)
        self.assertLess(
            offsets.index(movement.P3_MOVEMENT_ACTIVE_OFFSET),
            offsets.index(movement.P3_ACTOR_OFFSET),
        )
        phys_writes = [
            data
            for address, data in self.process.writes
            if address == self.block + movement.P3_PHYS_OFFSET
        ]
        self.assertEqual(
            phys_writes,
            [struct.pack("<Q", 0), struct.pack("<Q", 0x3333)],
        )

    def test_ownership_is_dropped_before_a_stale_command_is_written(self):
        movement.write_third_player_command(self.process, 0x1234, 1.5, 0.12, False)
        released = mailbox.MovementCommand.from_payload(self.process.writes[4][1])
        self.assertFalse(released.active)
        offsets = [address - self.block for address, _data in self.process.writes[6:]]
        self.assertEqual(offsets[0], movement.P3_MOVEMENT_ACTIVE_OFFSET)

    def test_release_drops_ownership_before_the_identity(self):
        movement.clear_third_player_command(self.process)
        offsets = [address - self.block for address, _data in self.process.writes[6:]]
        self.assertEqual(offsets[0], movement.P3_MOVEMENT_ACTIVE_OFFSET)
        self.assertLess(
            offsets.index(movement.P3_MOVEMENT_ACTIVE_OFFSET),
            offsets.index(movement.P3_ACTOR_OFFSET),
        )

    def test_role_loss_hides_active_then_actor_and_phys_immediately(self):
        movement.write_third_player_command(
            self.process, 0x1234, 1.5, 0.12, True, phys=0x5678
        )
        self.process.writes.clear()
        movement.guard_p3_publication_generation(
            self.process,
            0x1234,
            False,
        )
        self.assertEqual(
            [
                (address - self.block, data)
                for address, data in self.process.writes[6:9]
            ],
            [
                (
                    movement.P3_MOVEMENT_ACTIVE_OFFSET,
                    struct.pack("<I", 0),
                ),
                (movement.P3_ACTOR_OFFSET, struct.pack("<Q", 0)),
                (movement.P3_PHYS_OFFSET, struct.pack("<Q", 0)),
            ],
        )

    def test_actor_generation_change_retires_old_private_mailbox(self):
        movement.write_third_player_command(
            self.process, 0x1111, 1.5, 0.12, True, phys=0x5678
        )
        self.process.writes.clear()
        movement.guard_p3_publication_generation(
            self.process,
            0x2222,
            True,
        )
        offsets = [address - self.block for address, _data in self.process.writes[6:]]
        self.assertEqual(
            offsets[:3],
            [
                movement.P3_MOVEMENT_ACTIVE_OFFSET,
                movement.P3_ACTOR_OFFSET,
                movement.P3_PHYS_OFFSET,
            ],
        )

    def test_stable_owned_generation_does_not_interrupt_p3(self):
        movement.write_third_player_command(
            self.process, 0x1234, 1.5, 0.12, True, phys=0x5678
        )
        self.process.writes.clear()
        movement.guard_p3_publication_generation(
            self.process,
            0x1234,
            True,
        )
        self.assertEqual(self.process.writes, [])

    def test_release_leaves_an_actor_slot_the_cave_cannot_match(self):
        movement.clear_third_player_command(self.process)
        actor = dict(
            (address - self.block, data) for address, data in self.process.writes
        )[movement.P3_ACTOR_OFFSET]
        self.assertEqual(actor, b"\x00" * 8)

    def test_every_write_lands_inside_the_reserved_block(self):
        movement.write_third_player_command(
            self.process, 0x1234, 1.5, 0.12, True, phys=0x5678
        )
        for address, data in self.process.writes:
            in_atomic_mailbox = (
                movement.P3_MOVEMENT_MAILBOX <= address
                and address + len(data)
                <= movement.P3_MOVEMENT_MAILBOX + mailbox.MAILBOX_STORAGE_SIZE
            )
            in_legacy_block = (
                self.block <= address
                and address + len(data) <= self.block + movement.P3_COMMAND_BLOCK_SIZE
            )
            self.assertTrue(in_atomic_mailbox or in_legacy_block)


if __name__ == "__main__":
    unittest.main()
