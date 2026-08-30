"""Byte-level contract for the native follower action-approach gate."""

from __future__ import annotations

import unittest

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

import coop_runtime as movement


DUAL_BASE = 0x5C000000
LEGACY_BASE = 0x5BAA0000


def listing(code):
    decoder = Cs(CS_ARCH_X86, CS_MODE_64)
    return [
        (item.address, item.mnemonic, item.op_str, item.size)
        for item in decoder.disasm(code, 0)
    ]


class FakeProcess:
    def __init__(self, site_bytes):
        self.memory = {movement.FOLLOWER_ACTION_APPROACH_GATE_SITE: bytes(site_bytes)}
        self.writes = []
        self.patches = []

    def read(self, address, size):
        return self.memory.get(address, b"\x00" * size)[:size]

    def write(self, address, payload):
        payload = bytes(payload)
        self.memory[address] = payload
        self.writes.append((address, payload))

    def patch_code(self, address, payload):
        payload = bytes(payload)
        self.memory[address] = payload
        self.patches.append((address, payload))


class FollowerActionApproachCaveTests(unittest.TestCase):
    def setUp(self):
        movement.set_cave_base(LEGACY_BASE)
        movement.set_dual_interaction_base(DUAL_BASE)
        self.code = movement.build_follower_action_approach_gate_cave(
            movement.FOLLOWER_ACTION_APPROACH_GATE_CAVE,
            movement.COMMAND_BLOCK,
        )
        self.instructions = listing(self.code)
        self.text = " | ".join(
            f"{mnemonic} {operands}"
            for _address, mnemonic, operands, _size in self.instructions
        )

    def test_cave_fits_one_page_and_fully_disassembles(self):
        self.assertLessEqual(len(self.code), 0x100)
        consumed = sum(size for _a, _m, _o, size in self.instructions)
        self.assertEqual(consumed, len(self.code))

    def test_replays_the_native_handle_comparison_and_rbx_restore(self):
        self.assertIn("cmp eax, ebx", self.text)
        self.assertEqual(
            self.text.count("mov rbx, qword ptr [rsp + 0x30]"),
            3,
        )

    def test_controlled_players_jump_to_native_no_approach_epilogue(self):
        target = movement.FOLLOWER_ACTION_APPROACH_NO_APPROACH
        base = movement.FOLLOWER_ACTION_APPROACH_GATE_CAVE
        destinations = []
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        for instruction in decoder.disasm(self.code, base):
            if instruction.mnemonic == "jmp" and instruction.op_str.startswith("0x"):
                destinations.append(int(instruction.op_str, 16))
        self.assertIn(target, destinations)

    def test_only_aliases_during_an_active_coop_session(self):
        self.assertIn("cmp dword ptr [r11 + 8], 0", self.text)

    def test_matches_the_published_p2_and_p3_actor_pointers(self):
        self.assertIn("cmp rdi, qword ptr [r11 + 0x10]", self.text)
        p3_actor = movement.P3_COMMAND_BLOCK_OFFSET - 0x200 + movement.P3_ACTOR_OFFSET
        self.assertIn(
            f"cmp rdi, qword ptr [r11 + {p3_actor:#x}]",
            self.text,
        )

    def test_matches_p1_only_while_berserk_manual_movement_owns_the_actor(self):
        p1_state = movement.P1_BERSERK_MOVEMENT_STATE_OFFSET - 0x200
        self.assertIn(
            "cmp dword ptr "
            f"[r11 + {p1_state + movement.P1_BERSERK_ACTIVE_OFFSET:#x}], 0",
            self.text,
        )
        self.assertIn(
            "cmp rdi, qword ptr "
            f"[r11 + {p1_state + movement.P1_BERSERK_ACTOR_OFFSET:#x}]",
            self.text,
        )

    def test_native_follower_exit_deliberately_leaves_zf_clear(self):
        self.assertIn("test rdi, rdi", self.text)

    def test_every_local_branch_lands_on_an_instruction_boundary(self):
        boundaries = {address for address, _m, _o, _s in self.instructions}
        boundaries.add(len(self.code))
        for _address, mnemonic, operands, _size in self.instructions:
            if mnemonic in ("je", "jne") and operands.startswith("0x"):
                target = int(operands, 16)
                if target <= len(self.code):
                    self.assertIn(target, boundaries)


class FollowerNativeApproachHelperTests(unittest.TestCase):
    def setUp(self):
        self.code = movement.build_follower_action_native_approach_helper()
        self.instructions = listing(self.code)
        self.text = " | ".join(
            f"{mnemonic} {operands}"
            for _address, mnemonic, operands, _size in self.instructions
        )

    def test_helper_fits_the_second_half_of_the_reserved_page(self):
        self.assertLessEqual(len(self.code), 0x80)
        consumed = sum(size for _a, _m, _o, size in self.instructions)
        self.assertEqual(consumed, len(self.code))

    def test_helper_requires_no_manual_input_queued_target_and_gambit(self):
        self.assertIn("test r10d, r10d", self.text)
        self.assertIn("cmp byte ptr [rax + 0x6b4], 4", self.text)
        self.assertIn("cmp dword ptr [rax + 0x710], 0", self.text)
        self.assertIn("mov rax, qword ptr [rax + 0x698]", self.text)
        self.assertIn("test byte ptr [rax], 4", self.text)

    def test_helper_has_explicit_true_and_false_returns(self):
        self.assertIn("mov eax, 1", self.text)
        self.assertIn("xor eax, eax", self.text)
        self.assertEqual(
            sum(1 for _a, mnemonic, _o, _s in self.instructions if mnemonic == "ret"),
            2,
        )

    def test_installer_writes_only_the_helper_slot(self):
        movement.set_dual_interaction_base(DUAL_BASE)
        process = FakeProcess(movement.FOLLOWER_ACTION_APPROACH_GATE_ORIGINAL)

        movement.install_follower_action_native_approach_helper(process)

        self.assertEqual(
            process.writes,
            [
                (
                    movement.FOLLOWER_ACTION_NATIVE_APPROACH_HELPER,
                    bytes(self.code),
                )
            ],
        )
        self.assertEqual(process.patches, [])


class FollowerActionApproachInstallTests(unittest.TestCase):
    def setUp(self):
        movement.set_cave_base(LEGACY_BASE)
        movement.set_dual_interaction_base(DUAL_BASE)

    def test_install_writes_cave_before_patching_the_native_site(self):
        process = FakeProcess(movement.FOLLOWER_ACTION_APPROACH_GATE_ORIGINAL)

        movement.install_follower_action_approach_gate(process)

        self.assertEqual(
            process.writes[0][0],
            movement.FOLLOWER_ACTION_APPROACH_GATE_CAVE,
        )
        self.assertEqual(
            process.patches,
            [
                (
                    movement.FOLLOWER_ACTION_APPROACH_GATE_SITE,
                    movement.follower_action_approach_gate_patch(
                        movement.FOLLOWER_ACTION_APPROACH_GATE_CAVE
                    ),
                )
            ],
        )

    def test_install_refuses_unknown_live_bytes(self):
        process = FakeProcess(b"\x90" * 7)
        with self.assertRaisesRegex(RuntimeError, "unexpected bytes"):
            movement.install_follower_action_approach_gate(process)

    def test_restore_puts_back_the_exact_native_sequence(self):
        patch = movement.follower_action_approach_gate_patch(
            movement.FOLLOWER_ACTION_APPROACH_GATE_CAVE
        )
        process = FakeProcess(patch)

        movement.restore_follower_action_approach_gate(process)

        self.assertEqual(
            process.patches,
            [
                (
                    movement.FOLLOWER_ACTION_APPROACH_GATE_SITE,
                    movement.FOLLOWER_ACTION_APPROACH_GATE_ORIGINAL,
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
