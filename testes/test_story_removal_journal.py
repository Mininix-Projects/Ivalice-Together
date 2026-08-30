from __future__ import annotations

import struct
import unittest

import pytest

import story_removal_journal as journal


class FakeProcess:
    def __init__(self):
        self.memory: dict[int, int] = {}
        self.patches: list[tuple[int, bytes]] = []

    def read(self, address: int, size: int) -> bytes:
        return bytes(self.memory.get(address + offset, 0) for offset in range(size))

    def write(self, address: int, data: bytes) -> None:
        for offset, value in enumerate(bytes(data)):
            self.memory[address + offset] = value

    def patch_code(self, address: int, data: bytes) -> None:
        self.patches.append((address, bytes(data)))
        self.write(address, data)


class FailingProcess(FakeProcess):
    def __init__(self, fail_at: int):
        super().__init__()
        self.fail_at = int(fail_at)
        self.mutations = 0

    def _before_mutation(self) -> None:
        self.mutations += 1
        if self.mutations == self.fail_at:
            raise OSError(299, "injected interrupted installation")

    def write(self, address: int, data: bytes) -> None:
        self._before_mutation()
        super().write(address, data)

    def patch_code(self, address: int, data: bytes) -> None:
        self._before_mutation()
        FakeProcess.write(self, address, data)


class StoryRemovalJournalTests(unittest.TestCase):
    def test_cave_is_counter_only_then_tail_jumps_to_native_function(self):
        state = 0x50000000
        code = journal.build_story_removal_cave(0x50000100, state)
        self.assertEqual(code[:2], b"\x48\xb8")
        self.assertEqual(struct.unpack_from("<Q", code, 2)[0], state)
        self.assertIn(b"\x83\xf9\x10\x73\x04", code)
        self.assertIn(b"\xf0\xff\x04\x88", code)
        self.assertEqual(
            struct.unpack_from("<Q", code, code.rfind(b"\x48\xb8") + 2)[0],
            journal.REMOVE_PARTY_MEMBER,
        )
        self.assertTrue(code.endswith(b"\xff\xe0"))

    def test_call_patch_preserves_the_native_return_address(self):
        cave = 0x50000000
        patch = journal.build_story_removal_patch(cave)
        destination = (
            journal.SCRIPT_REMOVE_CALL + 5 + struct.unpack_from("<i", patch, 1)[0]
        )
        self.assertEqual(destination, cave)
        self.assertEqual(
            journal.SCRIPT_REMOVE_RETURN,
            journal.SCRIPT_REMOVE_CALL + len(patch),
        )

    def test_per_member_counters_do_not_lose_repeated_or_parallel_ids(self):
        previous = (0, 4, 9, 0) + (0,) * 12
        current = (0, 6, 9, 1) + (0,) * 12
        self.assertEqual(journal.changed_members(previous, current), (1, 3))
        self.assertEqual(
            journal.changed_members(
                (0xFFFFFFFF,) + (0,) * 15,
                (0,) * 16,
            ),
            (0,),
        )

    def test_install_decode_adopt_and_restore_are_process_lifetime_exact(self):
        process = FakeProcess()
        process.write(
            journal.SCRIPT_REMOVE_CALL,
            journal.SCRIPT_REMOVE_CALL_ORIGINAL,
        )
        first = journal.StoryRemovalJournal(process, 0x50000000)
        self.assertEqual(
            journal.decode_story_removal_hook(
                process, process.read(journal.SCRIPT_REMOVE_CALL, 5)
            ),
            (first.cave, first.state),
        )
        adopted = journal.StoryRemovalJournal(process, 0x50000000)
        self.assertEqual((adopted.cave, adopted.state), (first.cave, first.state))
        adopted.restore()
        self.assertEqual(
            process.read(journal.SCRIPT_REMOVE_CALL, 5),
            first.patch,
        )
        first.restore()
        self.assertEqual(
            process.read(journal.SCRIPT_REMOVE_CALL, 5),
            first.patch,
        )

    def test_active_hook_cannot_be_adopted_from_an_unreserved_workspace(self):
        process = FakeProcess()
        process.write(
            journal.SCRIPT_REMOVE_CALL,
            journal.SCRIPT_REMOVE_CALL_ORIGINAL,
        )
        journal.StoryRemovalJournal(process, 0x50000000)

        with self.assertRaisesRegex(RuntimeError, "reserved workspace"):
            journal.StoryRemovalJournal(process, 0x51000000)

    def test_interrupted_preparation_is_idempotently_completed(self):
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at):
                process = FailingProcess(fail_at)
                FakeProcess.write(
                    process,
                    journal.SCRIPT_REMOVE_CALL,
                    journal.SCRIPT_REMOVE_CALL_ORIGINAL,
                )
                process.mutations = 0
                with self.assertRaises(OSError):
                    journal.StoryRemovalJournal(process, 0x50000000)

                process.fail_at = -1
                recovered = journal.StoryRemovalJournal(process, 0x50000000)
                self.assertEqual(
                    journal.decode_story_removal_hook(
                        process,
                        process.read(journal.SCRIPT_REMOVE_CALL, 5),
                    ),
                    (recovered.cave, recovered.state),
                )

    def test_stock_root_rejects_foreign_state_or_cave_bytes(self):
        for offset in (
            journal.DEFAULT_STATE_OFFSET + journal.STATE_REGION_SIZE - 1,
            journal.DEFAULT_CAVE_OFFSET + journal.CAVE_REGION_SIZE - 1,
        ):
            with self.subTest(offset=offset):
                process = FakeProcess()
                process.write(
                    journal.SCRIPT_REMOVE_CALL,
                    journal.SCRIPT_REMOVE_CALL_ORIGINAL,
                )
                process.write(0x50000000 + offset, b"\x7f")

                with self.assertRaisesRegex(RuntimeError, "neither empty"):
                    journal.StoryRemovalJournal(process, 0x50000000)


@pytest.mark.parametrize("operation", ("state", "cave", "root"))
@pytest.mark.parametrize("timing", ("before", "after"))
def test_every_story_journal_commit_boundary_is_recoverable(operation, timing):
    class BoundaryProcess(FakeProcess):
        def __init__(self):
            super().__init__()
            self.failure = (operation, timing)
            self.events = []

        def mutate(self, kind, address, data):
            payload = bytes(data)
            self.events.append((kind, address, payload))
            selected = (
                operation == "state"
                and kind == "write"
                and address == workspace + journal.DEFAULT_STATE_OFFSET
            ) or (
                operation == "cave"
                and kind == "write"
                and address == workspace + journal.DEFAULT_CAVE_OFFSET
            ) or (
                operation == "root"
                and kind == "patch"
                and address == journal.SCRIPT_REMOVE_CALL
            )
            if selected and self.failure == (operation, "before"):
                raise OSError("injected pre-mutation failure")
            FakeProcess.write(self, address, payload)
            if selected and self.failure == (operation, "after"):
                raise OSError("injected post-mutation failure")

        def write(self, address, data):
            self.mutate("write", address, data)

        def patch_code(self, address, data):
            self.mutate("patch", address, data)

    workspace = 0x50000000
    process = BoundaryProcess()
    FakeProcess.write(
        process,
        journal.SCRIPT_REMOVE_CALL,
        journal.SCRIPT_REMOVE_CALL_ORIGINAL,
    )

    with pytest.raises(OSError, match="injected"):
        journal.StoryRemovalJournal(process, workspace)

    cave = workspace + journal.DEFAULT_CAVE_OFFSET
    patch = journal.build_story_removal_patch(cave)
    root = process.read(journal.SCRIPT_REMOVE_CALL, len(patch))
    assert root in (journal.SCRIPT_REMOVE_CALL_ORIGINAL, patch)

    process.failure = None
    process.events.clear()
    recovered = journal.StoryRemovalJournal(process, workspace)
    assert journal.decode_story_removal_hook(process, recovered.patch) == (
        recovered.cave,
        recovered.state,
    )
    if root == patch:
        assert process.events == []


def test_hot_reattach_preserves_live_story_counters_and_writes_nothing():
    process = FakeProcess()
    workspace = 0x50000000
    process.write(journal.SCRIPT_REMOVE_CALL, journal.SCRIPT_REMOVE_CALL_ORIGINAL)
    installed = journal.StoryRemovalJournal(process, workspace)
    counters = (9, 4, 0xFFFFFFFF) + (0,) * 13
    process.write(installed.state, struct.pack("<16I", *counters))
    process.patches.clear()
    before = dict(process.memory)

    adopted = journal.StoryRemovalJournal(process, workspace)

    assert adopted.snapshot() == counters
    assert process.memory == before
    assert process.patches == []


def test_story_root_change_before_commit_is_not_overwritten():
    class RacingProcess(FakeProcess):
        def __init__(self):
            super().__init__()
            self.root_reads = 0

        def read(self, address, size):
            if address == journal.SCRIPT_REMOVE_CALL:
                self.root_reads += 1
                if self.root_reads == 2:
                    FakeProcess.write(self, address, b"\xcc" * 5)
            return super().read(address, size)

    process = RacingProcess()
    FakeProcess.write(
        process,
        journal.SCRIPT_REMOVE_CALL,
        journal.SCRIPT_REMOVE_CALL_ORIGINAL,
    )

    with pytest.raises(RuntimeError, match="root changed before commit"):
        journal.StoryRemovalJournal(process, 0x50000000)

    assert process.patches == []
    assert process.read(journal.SCRIPT_REMOVE_CALL, 5) == b"\xcc" * 5


if __name__ == "__main__":
    unittest.main()
