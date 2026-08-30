import struct
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))
import menu_builder_snapshot_trace as trace


def put_record(
    raw,
    index,
    *,
    sequence,
    descriptor,
    timestamp,
    caller=0x27AFD5,
    menu_type=0x0B,
    result=2,
    entries=(b"\x12\x00\x00\x02\x01\x00\x00\x00" * 2),
):
    base = trace.RECORDS_OFFSET + index * trace.RECORD_SIZE
    count = len(entries) // 8
    header = bytearray(trace.DESCRIPTOR_HEADER_SIZE)
    struct.pack_into("<I", header, 0x00, menu_type)
    struct.pack_into("<I", header, 0x08, 38)
    struct.pack_into("<I", header, 0x14, 3)
    struct.pack_into("<I", header, 0x40, count)
    struct.pack_into(
        "<IIQQQiIIIII",
        raw,
        base,
        sequence,
        menu_type,
        descriptor,
        timestamp,
        caller,
        result,
        count,
        count,
        0,
        trace.DESCRIPTOR_HEADER_SIZE,
        8,
    )
    raw[
        base + trace.RECORD_HEADER_OFFSET : base
        + trace.RECORD_HEADER_OFFSET
        + len(header)
    ] = header
    raw[
        base + trace.RECORD_ENTRIES_OFFSET : base
        + trace.RECORD_ENTRIES_OFFSET
        + len(entries)
    ] = entries


class BuilderSnapshotTraceTests(unittest.TestCase):
    def test_hook_covers_exact_epilogue_and_returns_to_ret(self):
        cave = 0x50000000 + trace.CAVE_OFFSET
        patch = trace.jump_patch(cave)
        self.assertEqual(len(trace.ORIGINAL_EPILOGUE), 16)
        self.assertEqual(len(patch), 16)
        self.assertEqual(patch[:6], b"\xff\x25\0\0\0\0")
        self.assertEqual(struct.unpack("<Q", patch[6:14])[0], cave)
        self.assertEqual(patch[14:], b"\x90\x90")

        code = trace.build_cave(cave, 0x50000000)
        self.assertTrue(code.startswith(b"\x9c\x50\x51\x52\x56\x57"))
        active_inc = b"\xf0\x41\xff\x43\x08"
        active_dec = b"\xf0\x41\xff\x4b\x08"
        self.assertEqual(code.count(active_inc), 1)
        self.assertEqual(code.count(active_dec), 1)
        self.assertLess(code.index(active_inc), code.index(b"\x0f\x31"))
        self.assertLess(code.index(active_dec), code.index(trace.ORIGINAL_EPILOGUE))
        sequence_at = code.index(b"\x89\xd1\xff\xc1")
        scale_at = code.index(b"\x48\x69\xd2" + struct.pack("<I", trace.RECORD_SIZE))
        self.assertLess(sequence_at, scale_at)
        self.assertEqual(code[-30:-14], trace.ORIGINAL_EPILOGUE)
        self.assertEqual(code[-14:-8], b"\xff\x25\0\0\0\0")
        self.assertEqual(struct.unpack("<Q", code[-8:])[0], trace.HOOK_RETURN)

    def test_actor_and_caller_filters_branch_to_balanced_done_path(self):
        cave = 0x123456780000 + trace.CAVE_OFFSET
        code = trace.build_cave(
            cave,
            0x123456780000,
            actor_token=0x00200013,
            caller_filter=0x27E317,
        )
        actor_cmp = b"\x81\x7b\x08\x13\x00\x20\x00"
        caller_load = b"\x44\x8b\x94\x24\xf8\x00\x00\x00"
        caller_cmp = b"\x41\x81\xfa\x17\xe3\x27\x00"
        active_dec = b"\xf0\x41\xff\x4b\x08"
        self.assertIn(actor_cmp, code)
        self.assertIn(caller_load, code)
        self.assertIn(caller_cmp, code)
        self.assertEqual(code.count(active_dec), 1)
        self.assertLess(code.index(actor_cmp), code.index(active_dec))
        self.assertLess(code.index(caller_cmp), code.index(active_dec))

    def test_parser_and_exact_output_dedup_keep_real_variants(self):
        raw = bytearray(trace.RECORDS_OFFSET + 3 * trace.RECORD_SIZE)
        struct.pack_into("<II", raw, 0, 3, 0)
        put_record(raw, 0, sequence=1, descriptor=0x111000, timestamp=100)
        # Same completed output from a reused/different stack pointer: dedup.
        put_record(raw, 1, sequence=2, descriptor=0x222000, timestamp=200)
        # Same type/caller but different entry payload: must remain distinct.
        different = b"\x13\x00\x00\x08\x02\x00\x00\x00"
        put_record(
            raw,
            2,
            sequence=3,
            descriptor=0x222000,
            timestamp=300,
            entries=different,
        )

        parsed = trace.parse_trace_buffer(bytes(raw))
        self.assertEqual(parsed["stored"], 3)
        snapshots, events = trace.deduplicate_records(parsed["records"])
        self.assertEqual(len(events), 3)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0]["repeat_count"], 2)
        self.assertEqual(snapshots[1]["entries"][0]["dialog_id_u16_50"], 0x13)
        self.assertEqual(events[0]["snapshot_id"], events[1]["snapshot_id"])
        self.assertNotEqual(events[1]["snapshot_id"], events[2]["snapshot_id"])

    def test_parser_reports_overflow_and_clamps_attempted_count(self):
        raw = bytearray(trace.RECORDS_OFFSET + trace.RECORD_SIZE)
        struct.pack_into("<II", raw, 0, trace.MAX_RECORDS + 9, 9)
        # A short buffer is rightly rejected because MAX_RECORDS were claimed.
        with self.assertRaisesRegex(ValueError, "truncated"):
            trace.parse_trace_buffer(bytes(raw))

    def test_allocation_candidates_keep_cave_in_rel32_range(self):
        candidates = trace.allocation_candidates()
        for _ in range(20):
            base = next(candidates)
            displacement = base + trace.CAVE_OFFSET - (trace.HOOK_SITE + 5)
            self.assertGreaterEqual(displacement, -(1 << 31))
            self.assertLess(displacement, 1 << 31)

    def test_quiescence_waits_for_active_counter_to_drain(self):
        class FakeProcess:
            def __init__(self):
                self.values = [2, 1, 0]
                self.reads = 0

            def read(self, address, size):
                self.reads += 1
                value = self.values.pop(0) if self.values else 0
                return struct.pack("<I", value)

        process = FakeProcess()
        trace.wait_for_quiescence(
            process,
            0x50000000,
            timeout=0.1,
            stable_seconds=0.0,
            poll_seconds=0.0,
        )
        self.assertGreaterEqual(process.reads, 3)


if __name__ == "__main__":
    unittest.main()
