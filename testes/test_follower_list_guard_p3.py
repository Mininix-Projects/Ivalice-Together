"""Cover for the follower guard the runtime actually installs.

The older 0x305C71 call-site guard is retired: its cave reads as zeros in the
live game.  Leader-follow suppression happens in the list evaluator, so that
is where a third controlled actor has to be recognised.
"""

import unittest

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

import coop_runtime as movement


EVALUATOR_PROLOGUE = b"\x48\x89\x9c\x24\x80\x00\x00\x00"


class FakeProcess:
    def __init__(self, site):
        self.site = site
        self.caves = {}

    def read(self, address, size):
        if address == self.site:
            return EVALUATOR_PROLOGUE
        return bytes(size)

    def write(self, address, data):
        self.caves[address] = bytes(data)

    def patch_code(self, address, data):
        pass


class LiveFollowerListGuardTests(unittest.TestCase):
    def setUp(self):
        movement.set_cave_base(0x5BAA0000)
        process = FakeProcess(movement.FOLLOWER_LIST_EVALUATOR_SITE)
        movement.install_follower_list_guard(process)
        self.cave = process.caves[movement.FOLLOWER_LIST_GUARD_CAVE]
        md = Cs(CS_ARCH_X86, CS_MODE_64)
        self.listing = [
            (item.address, item.mnemonic, item.op_str)
            for item in md.disasm(bytes(self.cave), 0)
        ]
        self.text = " | ".join(f"{m} {o}" for _a, m, o in self.listing)

    def distance(self, field):
        return movement.P3_COMMAND_BLOCK_OFFSET - 0x200 + field

    def test_it_fits_its_region(self):
        self.assertLessEqual(len(self.cave), 0x180)

    def test_the_second_player_is_still_matched(self):
        self.assertIn("cmp r13, qword ptr [r11 + 0x10]", self.text)

    def test_the_third_player_is_matched_too(self):
        actor = self.distance(movement.P3_ACTOR_OFFSET)
        self.assertIn(f"cmp r13, qword ptr [r11 + {actor:#x}]", self.text)

    def test_an_unsteered_third_actor_keeps_native_following(self):
        active = self.distance(movement.P3_MOVEMENT_ACTIVE_OFFSET)
        self.assertIn(f"cmp dword ptr [r11 + {active:#x}], 0", self.text)

    def test_both_native_follow_lists_remain_the_only_targets(self):
        if movement.FOLLOWER_LIST_GUARD_REJECTS_EVERY_LIST:
            self.skipTest(
                "test switch is rejecting every list, not just the follow ones"
            )
        self.assertIn(f"{movement.FOLLOWER_LIST_PRIMARY:#x}", self.text)
        self.assertIn(f"{movement.FOLLOWER_LIST_SECONDARY:#x}", self.text)

    def test_flags_and_scratch_are_restored_on_both_exits(self):
        self.assertEqual(self.text.count("popfq"), 2)
        self.assertEqual(self.text.count("pop r11"), 2)

    def test_every_branch_lands_on_an_instruction_boundary(self):
        boundaries = {address for address, _m, _o in self.listing}
        boundaries.add(len(self.cave))
        for _address, mnemonic, op in self.listing:
            if mnemonic in ("je", "jne") and op.startswith("0x"):
                target = int(op, 16)
                if target <= len(self.cave):
                    self.assertIn(target, boundaries, f"bad branch to {op}")

    def test_the_guard_is_keyed_to_the_logical_session(self):
        # Capstone prints small displacements in decimal, so accept either
        # rendering rather than pinning the disassembler's formatting.
        offset = movement.COOP_ACTIVE_OFFSET
        self.assertTrue(
            f"cmp dword ptr [r11 + {offset}], 0" in self.text
            or f"cmp dword ptr [r11 + {offset:#x}], 0" in self.text,
            self.text,
        )


if __name__ == "__main__":
    unittest.main()
