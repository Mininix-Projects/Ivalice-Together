"""A scan pass that finds nothing must not erase the last publication.

Measured on the live process with three characters standing at a chest: the
aggregate slot carried a candidate for 0.013 ms out of every 16.67 ms frame -
0.08% of the time - because the snapshot copy ran unconditionally and an empty
pass immediately overwrote the populated one. The 120 Hz reader saw the prompt
roughly once every 650 frames, which is the reported "appears for one frame and
vanishes".

These tests pin the publication down at the two places it can regress: the cave
must skip the copy on an empty pass, and the reader must still be able to tell a
kept publication from an abandoned one.
"""

import struct
import unittest

import coop_runtime as coop
import headless_menu_shell as live


class WrittenCave:
    """Capture what install_dual_interaction_scan writes, without a game."""

    def __init__(self):
        self.data = {}
        self.regions = {}

    def read(self, address, size):
        return bytes(self.data.get(address + i, 0) for i in range(size))

    def write(self, address, payload):
        self.regions[address] = bytes(payload)
        for i, value in enumerate(payload):
            self.data[address + i] = value

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def patch_code(self, address, payload):
        self.write(address, payload)


def scan_cave() -> bytes:
    process = WrittenCave()
    process.write(
        coop.INTERACTION_DUAL_SCAN_CALL_SITE,
        coop.INTERACTION_DUAL_SCAN_CALL_ORIGINAL,
    )
    coop.install_dual_interaction_scan(process)
    return process.regions[coop.DUAL_INTERACTION_SCAN_CAVE]


class PublicationOffsetTests(unittest.TestCase):
    def test_counters_sit_past_the_published_snapshot(self):
        """0x120 bytes are copied, so the counters must start at 0x120."""

        self.assertEqual(coop.DUAL_INTERACTION_PUBLISH_TICK_OFFSET, 0x120)
        self.assertEqual(coop.DUAL_INTERACTION_PUBLISH_MARK_OFFSET, 0x124)

    def test_counters_do_not_reach_the_next_player(self):
        end = coop.DUAL_INTERACTION_P2_SNAPSHOT_OFFSET
        end += coop.DUAL_INTERACTION_PUBLISH_MARK_OFFSET + 4
        self.assertLessEqual(end, coop.DUAL_INTERACTION_P3_SNAPSHOT_OFFSET)

    def test_slot_span_matches_the_reader(self):
        self.assertEqual(
            coop.DUAL_INTERACTION_STATE_SLOTS_OFFSET,
            live.P2_INTERACTION_STATE_SLOTS_OFFSET,
        )
        span = coop.DUAL_INTERACTION_STATE_SLOT_COUNT * 4
        self.assertLessEqual(
            coop.DUAL_INTERACTION_STATE_SLOTS_OFFSET + span,
            live.P2_INTERACTION_HANDLE_SLOTS_OFFSET,
        )


class CaveShapeTests(unittest.TestCase):
    def setUp(self):
        self.cave = scan_cave()

    def test_the_copy_is_guarded(self):
        """rep movsq of the 0x120 snapshot must sit behind a conditional."""

        copy = b"\xb9\x24\x00\x00\x00\xf3\x48\xa5"
        self.assertIn(copy, self.cave)
        for at in self._positions(copy):
            window = self.cave[max(0, at - 0x40) : at]
            self.assertIn(b"\x0f\x84", window, "copia sem salto de guarda")

    def test_every_state_slot_is_tested(self):
        """Folded two slots per qword off a base biased to the slot array."""

        pairs = coop.DUAL_INTERACTION_STATE_SLOT_COUNT // 2
        for pair in range(pairs):
            probe = b"\x49\x0b\x4b" + bytes((pair * 8,))
            self.assertIn(probe, self.cave, f"par {pair} nao testado")
        self.assertEqual(
            len(self._positions(b"\x49\x0b\x4b")),
            pairs * 2,
            "os dois jogadores precisam do mesmo teste",
        )

    def test_the_empty_branch_lands_on_an_instruction_boundary(self):
        """Landing mid-instruction would execute garbage in the game thread."""

        stamp = b"\x41\x89\x8a" + struct.pack(
            "<I", coop.DUAL_INTERACTION_PUBLISH_MARK_OFFSET
        )
        marks = self._positions(stamp)
        self.assertTrue(marks)
        targets = set()
        for at in self._positions(b"\x0f\x84"):
            relative = struct.unpack_from("<i", self.cave, at + 2)[0]
            targets.add(at + 6 + relative)
        for at in marks:
            self.assertIn(
                at + len(stamp),
                targets,
                "nenhum salto vazio cai logo apos o carimbo",
            )

    def test_the_tick_advances_on_every_pass(self):
        probe = b"\x41\xff\x82" + struct.pack(
            "<I", coop.DUAL_INTERACTION_PUBLISH_TICK_OFFSET
        )
        self.assertEqual(len(self._positions(probe)), 2, "um inc por jogador")

    def test_the_tick_is_outside_the_guard(self):
        """It has to advance even when the pass publishes nothing."""

        tick = b"\x41\xff\x82" + struct.pack(
            "<I", coop.DUAL_INTERACTION_PUBLISH_TICK_OFFSET
        )
        first_guard = self.cave.find(b"\x0f\x84")
        self.assertGreater(first_guard, 0)
        self.assertLess(self._positions(tick)[0], first_guard)

    def _positions(self, needle):
        found = []
        at = self.cave.find(needle)
        while at >= 0:
            found.append(at)
            at = self.cave.find(needle, at + 1)
        return found


class StaleReadingTests(unittest.TestCase):
    def test_never_published_reads_as_absent(self):
        self.assertEqual(live.INTERACTION_PUBLISH_STALE_PASSES, 4)

    def test_a_kept_publication_is_still_current(self):
        for gap in (0, 1, live.INTERACTION_PUBLISH_STALE_PASSES):
            self.assertFalse(gap > live.INTERACTION_PUBLISH_STALE_PASSES, gap)

    def test_an_abandoned_publication_expires(self):
        gap = live.INTERACTION_PUBLISH_STALE_PASSES + 1
        self.assertGreater(gap, live.INTERACTION_PUBLISH_STALE_PASSES)

    def test_expiry_happens_inside_the_draw_grace(self):
        """Otherwise the prompt would linger past the grace and then blink."""

        passes_per_frame = 2
        seconds = live.INTERACTION_PUBLISH_STALE_PASSES / passes_per_frame / 60.0
        self.assertLess(seconds, live.INTERACTION_PROMPT_GRACE_SECONDS)


if __name__ == "__main__":
    unittest.main()
