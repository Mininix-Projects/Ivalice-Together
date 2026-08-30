"""Byte-level contract for the native P2/P3 gateway collision filter."""

from __future__ import annotations

import unittest

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

import coop_runtime as movement


CAVE = 0x5C00B100
COMMAND_BLOCK = 0x5BAA0200
THIRD_BLOCK = 0x5BAA8200


def listing(code):
    decoder = Cs(CS_ARCH_X86, CS_MODE_64)
    return [
        (item.address, item.mnemonic, item.op_str, item.size)
        for item in decoder.disasm(code, 0)
    ]


class FakeProcess:
    def __init__(self, site_bytes):
        self.memory = {
            movement.FOLLOWER_GATEWAY_COLLISION_FILTER_SITE: bytes(site_bytes)
        }
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


class FollowerGatewayCollisionCaveTests(unittest.TestCase):
    def setUp(self):
        movement.set_dual_interaction_base(0x5C000000)
        self.code = movement.build_follower_gateway_collision_filter_cave(
            CAVE,
            COMMAND_BLOCK,
            THIRD_BLOCK,
        )
        self.instructions = listing(self.code)
        self.text = " | ".join(
            f"{mnemonic} {operands}"
            for _address, mnemonic, operands, _size in self.instructions
        )

    def test_cave_fits_its_reserved_page_and_fully_disassembles(self):
        self.assertLessEqual(len(self.code), 0x100)
        consumed = sum(size for _a, _m, _o, size in self.instructions)
        self.assertEqual(consumed, len(self.code))

    def test_replays_the_exact_stock_role_store(self):
        self.assertIn(
            "mov word ptr [rcx + 0x80], bx",
            self.text,
        )

    def test_only_translates_native_follower_class_one(self):
        self.assertIn("cmp bx, 1", self.text)
        self.assertEqual(self.text.count("xor ebx, ebx"), 1)

    def test_matches_the_berserk_leader_and_both_follower_contexts(self):
        self.assertIn("mov r10, qword ptr [r11 + 0x48]", self.text)
        p3_displacement = THIRD_BLOCK + movement.P3_SKELETON_KEEP_OFFSET - COMMAND_BLOCK
        self.assertIn(
            f"mov r10, qword ptr [r11 + {p3_displacement:#x}]",
            self.text,
        )
        p1_berserk_state = (
            COMMAND_BLOCK + movement.P1_BERSERK_MOVEMENT_STATE_OFFSET - 0x200
        )
        self.assertIn(f"movabs r10, {p1_berserk_state:#x}", self.text)
        self.assertEqual(self.text.count("cmp rcx, r10"), 3)

    def test_preserves_flags_and_scratch_registers(self):
        mnemonics = [m for _a, m, _o, _s in self.instructions]
        self.assertEqual(mnemonics.count("pushfq"), 1)
        self.assertEqual(mnemonics.count("popfq"), 1)
        pushes = [o for _a, m, o, _s in self.instructions if m == "push"]
        pops = [o for _a, m, o, _s in self.instructions if m == "pop"]
        self.assertEqual(pushes, ["r10", "r11"])
        self.assertEqual(pops, ["r11", "r10"])


class FollowerGatewayCollisionInstallTests(unittest.TestCase):
    def setUp(self):
        movement.set_dual_interaction_base(0x5C000000)

    def test_install_writes_cave_then_patches_stock_site(self):
        process = FakeProcess(movement.FOLLOWER_GATEWAY_COLLISION_FILTER_ORIGINAL)
        movement.install_follower_gateway_collision_filter(process)

        self.assertEqual(
            process.writes[0][0],
            movement.FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE,
        )
        self.assertEqual(
            process.patches,
            [
                (
                    movement.FOLLOWER_GATEWAY_COLLISION_FILTER_SITE,
                    movement.follower_gateway_collision_filter_patch(
                        movement.FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE
                    ),
                )
            ],
        )

    def test_install_refuses_an_unknown_existing_patch(self):
        process = FakeProcess(b"\x90" * 5)
        with self.assertRaisesRegex(RuntimeError, "unexpected bytes"):
            movement.install_follower_gateway_collision_filter(process)

    def test_restore_puts_back_the_exact_stock_instruction(self):
        patched = movement.follower_gateway_collision_filter_patch(
            movement.FOLLOWER_GATEWAY_COLLISION_FILTER_CAVE
        )
        process = FakeProcess(patched)

        movement.restore_follower_gateway_collision_filter(process)

        self.assertEqual(
            process.patches,
            [
                (
                    movement.FOLLOWER_GATEWAY_COLLISION_FILTER_SITE,
                    movement.FOLLOWER_GATEWAY_COLLISION_FILTER_ORIGINAL,
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
