import struct
import unittest
from types import SimpleNamespace

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

import coop_runtime as movement


def disassemble(code, address):
    decoder = Cs(CS_ARCH_X86, CS_MODE_64)
    return [
        (item.address, item.mnemonic, item.op_str)
        for item in decoder.disasm(bytes(code), address)
    ]


class StatusProcess:
    def __init__(self, actor, battle_keep, status, permanent=0):
        self.values = {
            actor + 0x698: battle_keep,
            battle_keep + movement.TEMPORARY_STATUS_EFFECTS_OFFSET: status,
            battle_keep + movement.PERMANENT_STATUS_EFFECTS_OFFSET: permanent,
        }

    def u64(self, address):
        if address not in self.values:
            raise OSError("torn generation")
        return self.values[address]

    def u32(self, address):
        if address not in self.values:
            raise OSError("torn generation")
        return self.values[address] & 0xFFFFFFFF


class MailboxProcess:
    def __init__(self):
        self.memory = {}
        self.writes = []

    def read(self, address, size):
        return bytes(self.memory.get(address + index, 0) for index in range(size))

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def write(self, address, data):
        payload = bytes(data)
        self.writes.append((address, payload))
        for index, value in enumerate(payload):
            self.memory[address + index] = value


class BerserkLeaderMovementTests(unittest.TestCase):
    def setUp(self):
        movement.set_cave_base(0x5BAA0000)

    def test_native_berserk_bit_is_sampled_without_guessing(self):
        actor = 0x100000
        battle_keep = 0x200000
        self.assertTrue(
            movement.actor_has_berserk(
                StatusProcess(actor, battle_keep, movement.BERSERK_STATUS_MASK),
                actor,
            )
        )
        self.assertFalse(
            movement.actor_has_berserk(
                StatusProcess(actor, battle_keep, 1 << 26),
                actor,
            )
        )
        self.assertIsNone(movement.actor_has_berserk(StatusProcess(1, 2, 0), actor))

    def test_equipment_granted_berserk_uses_the_permanent_effect_word(self):
        actor = 0x100000
        battle_keep = 0x200000
        process = StatusProcess(
            actor,
            battle_keep,
            0,
            permanent=movement.BERSERK_STATUS_MASK,
        )

        self.assertTrue(movement.actor_has_berserk(process, actor))

    def test_ownership_requires_the_live_berserk_native_leader(self):
        valid = [True, True, True, 4, 4, True, True]
        self.assertTrue(movement.berserk_leader_owns_movement(*valid))
        for index in (0, 1, 2, 5, 6):
            rejected = list(valid)
            rejected[index] = False
            with self.subTest(index=index):
                self.assertFalse(movement.berserk_leader_owns_movement(*rejected))
        wrong_leader = list(valid)
        wrong_leader[3] = 5
        self.assertFalse(movement.berserk_leader_owns_movement(*wrong_leader))

    def test_solo_field_can_own_berserk_without_a_follower(self):
        snapshot = SimpleNamespace(
            p1_gameplay_ready=True,
            user_control_enabled=True,
            native_menu_open=False,
            native_modal_active=False,
            loading=False,
            cutscene=False,
        )
        logical, follower_movement, _camera = movement.coop_lifecycle_flags(
            True,
            True,
            None,
            None,
            p2_present=False,
            p3_present=False,
        )
        self.assertFalse(follower_movement)
        p1_active, p1_input = movement.coop_movement_control_flags(
            logical,
            movement.p1_session_movement_allowed(snapshot),
            False,
            False,
        )
        self.assertEqual((p1_active, p1_input), (True, True))
        self.assertTrue(
            movement.berserk_leader_owns_movement(
                p1_active,
                True,
                True,
                4,
                4,
                True,
                True,
            )
        )

    def test_berserk_steering_reads_the_native_p1_stick(self):
        class Bindings:
            def native_left_stick_axes(self, player):
                self.player = player
                return 1.0, 0.0

            def signed_axis(self, *_args):
                raise AssertionError("P1 has no configured movement actions")

        bindings = Bindings()
        heading, magnitude, moving = movement.player_steering(
            bindings,
            "p1",
            ((0.0, 1.0), (-1.0, 0.0)),
            False,
            True,
            native_leader_pad=True,
        )
        self.assertEqual(bindings.player, "p1")
        self.assertAlmostEqual(heading, 0.0)
        self.assertGreater(magnitude, 0.0)
        self.assertTrue(moving)

    def test_publication_claims_active_last_and_releases_first(self):
        process = MailboxProcess()
        actor = 0x12340000
        phys = 0x56780000
        interaction_entry = 0x34560000
        skeleton_keep = 0x45670000
        movement.write_p1_berserk_command(
            process,
            actor,
            phys,
            1.25,
            0.10,
            True,
            interaction_entry=interaction_entry,
            skeleton_keep=skeleton_keep,
        )
        active_address = (
            movement.P1_BERSERK_MOVEMENT_STATE + movement.P1_BERSERK_ACTIVE_OFFSET
        )
        actor_address = (
            movement.P1_BERSERK_MOVEMENT_STATE + movement.P1_BERSERK_ACTOR_OFFSET
        )
        phys_address = (
            movement.P1_BERSERK_MOVEMENT_STATE + movement.P1_BERSERK_PHYS_OFFSET
        )
        self.assertEqual(process.writes[0], (active_address, struct.pack("<I", 0)))
        self.assertEqual(process.writes[-1], (active_address, struct.pack("<I", 1)))
        self.assertEqual(process.u64(actor_address), actor)
        self.assertEqual(process.u64(phys_address), phys)
        self.assertEqual(
            process.u64(
                movement.P1_BERSERK_MOVEMENT_STATE
                + movement.P1_BERSERK_INTERACTION_ENTRY_OFFSET
            ),
            interaction_entry,
        )
        self.assertEqual(
            process.u64(
                movement.P1_BERSERK_MOVEMENT_STATE
                + movement.P1_BERSERK_SKELETON_KEEP_OFFSET
            ),
            skeleton_keep,
        )
        self.assertAlmostEqual(
            struct.unpack(
                "<f",
                process.read(
                    movement.P1_BERSERK_MOVEMENT_STATE
                    + movement.P1_BERSERK_VISUAL_HEADING_OFFSET,
                    4,
                ),
            )[0],
            1.25,
        )

        process.writes.clear()
        movement.clear_p1_berserk_command(process)
        self.assertEqual(process.writes[0], (active_address, struct.pack("<I", 0)))
        self.assertEqual(process.u64(actor_address), 0)
        self.assertEqual(process.u64(phys_address), 0)
        self.assertEqual(
            process.u64(
                movement.P1_BERSERK_MOVEMENT_STATE
                + movement.P1_BERSERK_INTERACTION_ENTRY_OFFSET
            ),
            0,
        )
        self.assertEqual(
            process.u64(
                movement.P1_BERSERK_MOVEMENT_STATE
                + movement.P1_BERSERK_SKELETON_KEEP_OFFSET
            ),
            0,
        )

    def test_native_helper_is_generation_scoped_and_writes_only_locomotion(self):
        code = movement.build_p1_berserk_movement_helper(movement.COMMAND_BLOCK)
        self.assertLessEqual(len(code), movement.P1_BERSERK_MOVEMENT_HELPER_SIZE)
        listing = disassemble(code, movement.P1_BERSERK_MOVEMENT_HELPER)
        text = " | ".join(f"{mnemonic} {operand}" for _, mnemonic, operand in listing)
        state = movement.P1_BERSERK_MOVEMENT_STATE_OFFSET - 0x200
        self.assertIn(f"[r11 + {state + movement.P1_BERSERK_ACTOR_OFFSET:#x}]", text)
        self.assertIn(f"[r11 + {state + movement.P1_BERSERK_PHYS_OFFSET:#x}]", text)
        for field in (0xA4, 0xD0, 0xA0):
            self.assertIn(f"dword ptr [rax + {field:#x}]", text)
        self.assertIn("mov edx, r10d", text)
        self.assertIn(
            f"cmp rax, qword ptr [r11 + {state + movement.P1_BERSERK_ACTOR_OFFSET:#x}]",
            text,
        )
        self.assertIn("mov r10, qword ptr [rsp]", text)
        self.assertNotIn("mov rax, qword ptr [rax + 0x10]", text)
        mnemonics = [mnemonic for _, mnemonic, _ in listing]
        self.assertIn("stc", mnemonics)
        self.assertIn("clc", mnemonics)
        self.assertEqual(mnemonics.count("ret"), 2)

    def test_shared_movement_cave_checks_berserk_before_followers(self):
        cave = movement.build_movement_cave(
            movement.COMMAND_BLOCK,
            bytes.fromhex("40 53 56 41 56"),
        )
        listing = disassemble(cave, movement.MOVEMENT_CAVE)
        call_index = next(
            index
            for index, (_address, mnemonic, operand) in enumerate(listing)
            if mnemonic == "call"
            and int(operand, 16) == movement.P1_BERSERK_MOVEMENT_HELPER
        )
        self.assertEqual(listing[call_index + 1][1], "jb")
        manual_actor_recheck = next(
            address
            for address, mnemonic, operand in listing
            if mnemonic == "cmp" and operand == "r10, qword ptr [rcx + 0x30]"
        )
        self.assertEqual(int(listing[call_index + 1][2], 16), manual_actor_recheck)
        p2_active_compare = next(
            index
            for index, (_address, mnemonic, operand) in enumerate(listing)
            if mnemonic == "cmp" and "[r11 + 0x38]" in operand
        )
        self.assertLess(call_index, p2_active_compare)


if __name__ == "__main__":
    unittest.main()
