"""The routed follower claim must mirror FF12's own interaction suppression.

Disassembling the stock interaction update (0x25C830..0x25D45D) showed it tests
no action or animation field: its entire suppression is two globals. The wrapper
already inferred "the leader is animating" from actor bits, which is a proxy; a
state that suppresses the stock prompt without touching those bits used to leave
a routed follower still able to claim, and the leader then owed an animation it
could not play. These tests pin the real predicate in place.
"""

import struct
import unittest

import headless_menu_shell as live


MAILBOX = 0x10000300


def wrapper() -> bytes:
    return live.build_p2_interaction_consumer_wrapper(MAILBOX)


def conditional_targets(code: bytes) -> dict[int, list[int]]:
    """Map every near-jcc destination to the offsets that jump there."""

    targets: dict[int, list[int]] = {}
    index = 0
    while index < len(code) - 6:
        if code[index] == 0x0F and code[index + 1] in (0x84, 0x85, 0x87):
            relative = struct.unpack_from("<i", code, index + 2)[0]
            targets.setdefault(index + 6 + relative, []).append(index)
            index += 6
            continue
        index += 1
    return targets


class SuppressionConstantTests(unittest.TestCase):
    def test_globals_match_the_disassembled_native_gate(self):
        self.assertEqual(live.NATIVE_INTERACTION_SUPPRESSION_FLAGS, 0x02089340)
        self.assertEqual(live.NATIVE_INTERACTION_SUPPRESSION_MASK, 0xF00)
        self.assertEqual(live.NATIVE_INTERACTION_EVENT_FLAGS, 0x01FD46D4)
        self.assertEqual(live.NATIVE_INTERACTION_EVENT_BUSY_MASK, 1 << 10)

    def test_event_mask_is_bit_ten(self):
        """0x226BD0 does bt with ecx=0x0A, so only bit 10 may be tested."""

        self.assertEqual(live.NATIVE_INTERACTION_EVENT_BUSY_MASK, 0x400)


class SuppressionGateTests(unittest.TestCase):
    def test_both_native_tests_are_emitted(self):
        code = wrapper()
        suppression = (
            struct.pack("<Q", live.NATIVE_INTERACTION_SUPPRESSION_FLAGS)
            + b"\xf7\x00"
            + struct.pack("<I", live.NATIVE_INTERACTION_SUPPRESSION_MASK)
        )
        event = (
            struct.pack("<Q", live.NATIVE_INTERACTION_EVENT_FLAGS)
            + b"\xf7\x00"
            + struct.pack("<I", live.NATIVE_INTERACTION_EVENT_BUSY_MASK)
        )
        self.assertIn(suppression, code)
        self.assertIn(event, code)

    def test_a_set_flag_rejects_rather_than_claims(self):
        """Both tests must branch on non-zero: a set bit means suppressed."""

        code = wrapper()
        for global_address, mask in (
            (
                live.NATIVE_INTERACTION_SUPPRESSION_FLAGS,
                live.NATIVE_INTERACTION_SUPPRESSION_MASK,
            ),
            (
                live.NATIVE_INTERACTION_EVENT_FLAGS,
                live.NATIVE_INTERACTION_EVENT_BUSY_MASK,
            ),
        ):
            test = (
                struct.pack("<Q", global_address)
                + b"\xf7\x00"
                + struct.pack("<I", mask)
            )
            at = code.find(test)
            self.assertGreaterEqual(at, 0, hex(global_address))
            jump = at + len(test)
            self.assertEqual(code[jump], 0x0F, hex(global_address))
            self.assertEqual(code[jump + 1], 0x85, hex(global_address))

    def test_both_gates_reject_through_the_existing_path(self):
        """A private exit would skip the wrapper's own cleanup."""

        code = wrapper()
        targets = conditional_targets(code)
        render_test = struct.pack("<Q", live.NATIVE_RENDERING_FLAGS) + b"\xf6\x00"
        render_at = code.find(render_test)
        self.assertGreaterEqual(render_at, 0)
        render_jump = render_at + len(render_test) + 1
        reject = next(
            destination
            for destination, sources in targets.items()
            if render_jump in sources
        )

        for global_address, mask in (
            (
                live.NATIVE_INTERACTION_SUPPRESSION_FLAGS,
                live.NATIVE_INTERACTION_SUPPRESSION_MASK,
            ),
            (
                live.NATIVE_INTERACTION_EVENT_FLAGS,
                live.NATIVE_INTERACTION_EVENT_BUSY_MASK,
            ),
        ):
            test = (
                struct.pack("<Q", global_address)
                + b"\xf7\x00"
                + struct.pack("<I", mask)
            )
            jump = code.find(test) + len(test)
            self.assertIn(
                jump,
                targets[reject],
                f"{global_address:#x} nao rejeita pelo caminho comum",
            )

    def test_gate_runs_before_the_generation_handle_is_resolved(self):
        """Suppression rejects before any follower entity is resolved."""

        code = wrapper()
        handle_load = code.find(
            b"\x8b\x4b" + bytes((live.P2_INTERACTION_CONSUMER_GATE_HANDLE_OFFSET,))
        )
        self.assertGreaterEqual(handle_load, 0)
        for global_address in (
            live.NATIVE_INTERACTION_SUPPRESSION_FLAGS,
            live.NATIVE_INTERACTION_EVENT_FLAGS,
        ):
            at = code.find(struct.pack("<Q", global_address))
            self.assertGreater(at, 0, hex(global_address))
            self.assertLess(at, handle_load, hex(global_address))

    def test_cave_still_fits_its_reserved_region(self):
        self.assertLessEqual(len(wrapper()), 0x200)

    def test_atomic_claim_and_reject_state_are_both_still_present(self):
        code = wrapper()
        self.assertIn(bytes.fromhex("B80100000041BA02000000F0440FB15308"), code)
        self.assertIn(bytes.fromhex("C7430803000000"), code)


if __name__ == "__main__":
    unittest.main()
