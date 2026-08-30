"""FF12's gateway trigger performs one pass for one character.

`FUN_0025B600` nao recebe parametro: le o handle do personagem controlado de um
global, resolve a entidade, pergunta ao objeto de colisao dela em que volume de
gatilho esta pisando e dispara o script daquele volume.  Nao existe laco de
party nem teste de volume por ator, entao nao ha filtro para alargar -- a unica
forma fiel de dar o mesmo gatilho a um follower e rodar a mesma passada de novo
com ele nomeado como controlado.

Estes testes afirmam o que o cave nao pode deixar de fazer: rodar a passada
nativa primeiro e intacta, nunca rodar duas vezes para o mesmo personagem, e
devolver o handle controlado ao valor original mesmo quando nenhum follower
existe.
"""

import struct
import unittest

import coop_runtime as movement


CAVE = 0x30000000
SCRATCH = CAVE + 0xE0
COMMAND_BLOCK = 0x31000000
P3_BLOCK = 0x31008200


def build():
    return movement.build_follower_gateway_trigger_cave(
        CAVE, SCRATCH, COMMAND_BLOCK, P3_BLOCK
    )


def call_targets(code):
    """Absolute targets of every E8 call in the cave."""

    targets = []
    for index, byte in enumerate(code):
        if byte != 0xE8 or index + 5 > len(code):
            continue
        displacement = struct.unpack_from("<i", code, index + 1)[0]
        targets.append(CAVE + index + 5 + displacement)
    return targets


def rip_targets(code):
    """Absolute targets of every RIP-relative operand the cave reads."""

    targets = set()
    for opcode, length in (
        (b"\x8b\x05", 6),
        (b"\x89\x05", 6),
        (b"\x3b\x05", 6),
        (b"\x48\x8b\x05", 7),
        (b"\x48\x89\x05", 7),
        (b"\x48\x8b\x0d", 7),
    ):
        start = 0
        while True:
            index = code.find(opcode, start)
            if index < 0:
                break
            start = index + 1
            displacement = struct.unpack_from("<i", code, index + len(opcode))[0]
            targets.add(CAVE + index + length + displacement)
    return targets


class FollowerGatewayTriggerCaveTests(unittest.TestCase):
    def test_native_pass_precedes_the_two_follower_passes(self):
        """P1 still runs before either extra follower pass."""

        code = build()
        targets = call_targets(code)
        first_native = targets.index(movement.GATEWAY_TRIGGER_NATIVE)
        self.assertEqual(first_native, 0)

    def test_pass_runs_once_per_player(self):
        """One native call plus one per follower -- no accidental repeats."""

        targets = call_targets(build())
        self.assertEqual(
            targets.count(movement.GATEWAY_TRIGGER_NATIVE),
            3,
            "expected the stock pass plus one pass for each follower",
        )
        self.assertEqual(
            targets.count(movement.ENTITY_HANDLE_RESOLVER),
            0,
            "the native gateway must resolve each stable handle itself",
        )

    def test_reads_both_published_follower_handles(self):
        """P2 and P3 are named by generation-checked handles, never pointers."""

        targets = rip_targets(build())
        self.assertIn(COMMAND_BLOCK + movement.P2_LOOT_HANDLE_OFFSET, targets)
        self.assertIn(P3_BLOCK + movement.P3_LOOT_HANDLE_OFFSET, targets)

    def test_poisoned_published_entries_are_never_loaded_or_resolved(self):
        """A despawned actor may leave any mailbox entry value unmapped."""

        code = build()
        targets = rip_targets(build())
        p1_entry = (
            COMMAND_BLOCK
            + movement.P1_BERSERK_MOVEMENT_STATE_OFFSET
            - 0x200
            + movement.P1_BERSERK_INTERACTION_ENTRY_OFFSET
        )
        self.assertNotIn(p1_entry, targets)
        self.assertNotIn(COMMAND_BLOCK + movement.P2_INTERACTION_ENTRY_OFFSET, targets)
        self.assertNotIn(P3_BLOCK + movement.P3_INTERACTION_ENTRY_OFFSET, targets)
        self.assertNotIn(movement.ENTITY_HANDLE_RESOLVER, call_targets(code))
        # No qword pointer load remains in the cave, so values such as the
        # dump-proven 0xBFB1ECCC can never become a memory base here.
        self.assertNotIn(b"\x48\x8b\x05", code)
        self.assertNotIn(b"\x48\x8b\x0d", code)

    def test_touches_only_the_controlled_handle_global(self):
        """The native pass resolves all other identity from this live handle."""

        targets = rip_targets(build())
        game_globals = {
            address
            for address in targets
            if address < 0x01000000 or (0x01000000 <= address < 0x03000000)
        }
        self.assertEqual(
            game_globals,
            {movement.CONTROLLED_HANDLE_GLOBAL},
        )

    def test_restores_the_controlled_handle_before_returning(self):
        """A follower pass must never leak its identity into the next frame."""

        code = build()
        targets = rip_targets(code)
        self.assertIn(SCRATCH, targets)
        self.assertNotIn(SCRATCH + 8, targets)
        # The last thing before the tail jump restores the handle after every
        # possible follower call.
        last_call = max(index for index, byte in enumerate(code) if byte == 0xE8)
        tail = code[last_call:]
        self.assertIn(b"\x89\x05", tail)

    def test_returns_to_the_instruction_after_the_patched_call(self):
        code = build()
        self.assertEqual(code[-5], 0xE9)
        displacement = struct.unpack_from("<i", code, len(code) - 4)[0]
        self.assertEqual(
            CAVE + len(code) + displacement, movement.GATEWAY_TRIGGER_RETURN
        )

    def test_patch_is_exactly_the_stock_call_width(self):
        """A wider patch would clobber the INC that follows the call."""

        patch = movement.follower_gateway_trigger_patch(CAVE)
        self.assertEqual(len(patch), len(movement.GATEWAY_TRIGGER_ORIGINAL))
        self.assertEqual(len(patch), 5)
        self.assertEqual(patch[0], 0xE9)

    def test_counts_how_far_each_follower_pass_got(self):
        """Diagnostics: which guard stopped the follower, not just pass/fail."""

        targets = rip_targets(build())
        code = build()
        counters = set()
        start = 0
        while True:
            index = code.find(b"\xff\x05", start)
            if index < 0:
                break
            start = index + 1
            displacement = struct.unpack_from("<i", code, index + 2)[0]
            counters.add(CAVE + index + 6 + displacement)
        self.assertEqual(
            counters,
            {
                SCRATCH + movement.GATEWAY_TRIGGER_COUNT_ACTIVE,
                SCRATCH + movement.GATEWAY_TRIGGER_COUNT_ENTITY,
                SCRATCH + movement.GATEWAY_TRIGGER_COUNT_HANDLE,
                SCRATCH + movement.GATEWAY_TRIGGER_COUNT_PASS,
            },
        )
        self.assertTrue(counters.issubset(targets | counters))

    def test_scratch_holds_saved_values_and_counters(self):
        size = movement.FOLLOWER_GATEWAY_TRIGGER_SCRATCH_SIZE
        for offset in (
            movement.GATEWAY_TRIGGER_SAVED_HANDLE,
            movement.GATEWAY_TRIGGER_COUNT_ACTIVE,
            movement.GATEWAY_TRIGGER_COUNT_ENTITY,
            movement.GATEWAY_TRIGGER_COUNT_HANDLE,
            movement.GATEWAY_TRIGGER_COUNT_PASS,
        ):
            self.assertLessEqual(offset + 4, size)

    def test_coop_gate_resolves_to_the_active_flag(self):
        """A RIP displacement is measured from the end of the instruction.

        The co-op check is `cmp dword [rip+d],0`, the one operand here with a
        trailing immediate.  Measuring to the end of the displacement instead
        shifts it by the immediate's width, which lands on the visual-heading
        bytes -- a gate that reads true on ~255 of every 256 frames and so
        looks like it works.
        """

        code = build()
        index = code.find(b"\x83\x3d")
        self.assertGreaterEqual(index, 0)
        displacement = struct.unpack_from("<i", code, index + 2)[0]
        end_of_instruction = index + 2 + 4 + 1  # opcode, disp32, imm8
        self.assertEqual(
            CAVE + end_of_instruction + displacement,
            COMMAND_BLOCK + 8,
        )

    def test_code_stops_before_its_own_scratch(self):
        self.assertLessEqual(
            len(build()),
            movement.FOLLOWER_GATEWAY_TRIGGER_SCRATCH_OFFSET
            - movement.FOLLOWER_GATEWAY_TRIGGER_CAVE_OFFSET,
        )


class FollowerGatewayTriggerSiteTests(unittest.TestCase):
    def test_site_is_the_call_the_dispatcher_makes(self):
        """0x25DED0 is the CALL 0x25B600 inside FUN_0025D9E0."""

        self.assertEqual(
            movement.GATEWAY_TRIGGER_RETURN,
            movement.GATEWAY_TRIGGER_SITE + 5,
        )
        displacement = struct.unpack_from("<i", movement.GATEWAY_TRIGGER_ORIGINAL, 1)[0]
        self.assertEqual(
            movement.GATEWAY_TRIGGER_SITE + 5 + displacement,
            movement.GATEWAY_TRIGGER_NATIVE,
        )

    def test_enabled_without_taking_over_the_npc_descriptor(self):
        """Door triggers stay enabled while the unsafe exact-NPC route stays off."""

        self.assertTrue(movement.FOLLOWER_GATEWAY_TRIGGER_ENABLED)
        self.assertFalse(movement.FOLLOWER_NPC_INTERACTIONS_ENABLED)


if __name__ == "__main__":
    unittest.main()
