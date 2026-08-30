import struct
import sys
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

import movement_mailbox as mailbox  # noqa: E402


class MemoryProcess:
    def __init__(self, base=0x1000):
        self.base = base
        self.memory = bytearray(mailbox.MAILBOX_STORAGE_SIZE)
        self.writes = []
        self.after_write = None

    def write(self, address, data):
        data = bytes(data)
        offset = address - self.base
        self.memory[offset : offset + len(data)] = data
        self.writes.append((address, data))
        if self.after_write is not None:
            self.after_write()

    def read(self, address, size):
        offset = address - self.base
        return bytes(self.memory[offset : offset + size])


def command(actor, generation, heading, magnitude, *, member=5, epoch=7):
    return mailbox.MovementCommand(
        True,
        actor,
        actor + 0x1000,
        member,
        generation,
        epoch,
        heading,
        heading + 0.125,
        magnitude,
    )


class MovementMailboxTests(unittest.TestCase):
    def test_publication_commits_fallback_then_odd_payload_even(self):
        process = MemoryProcess()
        publisher = mailbox.MovementMailboxPublisher(process, process.base)

        self.assertTrue(publisher.publish(command(0x2000, 1, 0.5, 1.25)))

        self.assertEqual(len(process.writes), 6)
        fallback_odd, fallback, fallback_even, odd, payload, even = process.writes
        self.assertEqual(
            fallback_odd[0], process.base + mailbox.FALLBACK_SEQUENCE_OFFSET
        )
        self.assertEqual(struct.unpack("<I", fallback_odd[1])[0] & 1, 1)
        self.assertEqual(
            fallback,
            (
                process.base + mailbox.FALLBACK_ACTIVE_OFFSET,
                b"\x00" * mailbox.FALLBACK_PAYLOAD_SIZE,
            ),
        )
        self.assertEqual(
            fallback_even[0], process.base + mailbox.FALLBACK_SEQUENCE_OFFSET
        )
        self.assertEqual(struct.unpack("<I", fallback_even[1])[0] & 1, 0)
        self.assertEqual(odd[0], process.base + mailbox.SEQUENCE_OFFSET)
        self.assertEqual(struct.unpack("<I", odd[1])[0] & 1, 1)
        self.assertEqual(payload[0], process.base + mailbox.ACTIVE_OFFSET)
        self.assertEqual(len(payload[1]), mailbox.MAILBOX_SIZE - 4)
        self.assertEqual(even[0], process.base + mailbox.SEQUENCE_OFFSET)
        self.assertEqual(struct.unpack("<I", even[1])[0] & 1, 0)

    def test_changed_publication_exposes_the_old_complete_heading_before_odd(self):
        process = MemoryProcess()
        publisher = mailbox.MovementMailboxPublisher(process, process.base)
        old = command(0x2000, 1, 0.25, 0.75)
        new = command(0x3000, 2, -1.25, 2.5)
        publisher.publish(old)
        old_sequence = publisher.sequence
        process.writes.clear()

        self.assertTrue(publisher.publish(new))

        self.assertEqual(
            [address - process.base for address, _data in process.writes],
            [
                mailbox.FALLBACK_SEQUENCE_OFFSET,
                mailbox.FALLBACK_ACTIVE_OFFSET,
                mailbox.FALLBACK_SEQUENCE_OFFSET,
                mailbox.SEQUENCE_OFFSET,
                mailbox.ACTIVE_OFFSET,
                mailbox.SEQUENCE_OFFSET,
            ],
        )
        active, source_sequence, actor, phys, heading, visual = struct.unpack(
            "<IIQQff", process.writes[1][1]
        )
        self.assertEqual(active, 1)
        self.assertEqual(source_sequence, old_sequence)
        self.assertEqual(actor, old.actor)
        self.assertEqual(phys, old.phys)
        self.assertAlmostEqual(heading, old.heading)
        self.assertAlmostEqual(visual, old.visual_heading)
        fallback_odd = struct.unpack("<I", process.writes[0][1])[0]
        fallback_even = struct.unpack("<I", process.writes[2][1])[0]
        self.assertEqual(fallback_odd, fallback_even | 1)
        self.assertEqual(
            struct.unpack("<I", process.writes[3][1])[0],
            (old_sequence + 2) | 1,
        )

    def test_lagging_fallback_reader_rejects_the_next_publication(self):
        process = MemoryProcess()
        publisher = mailbox.MovementMailboxPublisher(process, process.base)
        old = command(0x2000, 1, 0.25, 0.75)
        new = command(0x3000, 2, -1.25, 2.5)
        newest = command(0x4000, 3, 2.25, 1.5)
        publisher.publish(old)
        publisher.publish(new)

        first = struct.unpack(
            "<I",
            process.read(
                process.base + mailbox.FALLBACK_SEQUENCE_OFFSET,
                4,
            ),
        )[0]
        self.assertEqual(first & 1, 0)

        # The reader is descheduled here. Publication N+1 legitimately reuses
        # the fallback record, but its local sequence makes the old read fail.
        publisher.publish(newest)
        payload = process.read(
            process.base + mailbox.FALLBACK_ACTIVE_OFFSET,
            mailbox.FALLBACK_PAYLOAD_SIZE,
        )
        final = struct.unpack(
            "<I",
            process.read(
                process.base + mailbox.FALLBACK_SEQUENCE_OFFSET,
                4,
            ),
        )[0]

        self.assertNotEqual(first, final)
        self.assertEqual(struct.unpack("<IIQQff", payload)[2], new.actor)
        committed = mailbox.read_consistent(process, process.base)
        self.assertIsNotNone(committed)
        self.assertEqual(committed[1], newest)

    def test_reader_observes_old_new_or_unavailable_never_mixed(self):
        process = MemoryProcess()
        publisher = mailbox.MovementMailboxPublisher(process, process.base)
        old = command(0x2000, 1, 0.25, 0.75)
        new = command(0x3000, 2, -1.25, 2.5)
        publisher.publish(old)
        observations = []

        def sample():
            result = mailbox.read_consistent(process, process.base, attempts=1)
            observations.append(None if result is None else result[1])

        process.after_write = sample
        publisher.publish(new)

        self.assertEqual(observations[-1], new)
        self.assertTrue(
            all(item is None or item in (old, new) for item in observations)
        )
        self.assertNotIn(
            (new.heading, old.magnitude),
            [
                (item.heading, item.magnitude)
                for item in observations
                if item is not None
            ],
        )

    def test_identical_command_does_not_write_or_advance(self):
        process = MemoryProcess()
        publisher = mailbox.MovementMailboxPublisher(process, process.base)
        value = command(0x2000, 1, 0.5, 1.25)
        publisher.publish(value)
        process.writes.clear()
        sequence = publisher.sequence

        self.assertFalse(publisher.publish(value))

        self.assertEqual(process.writes, [])
        self.assertEqual(publisher.sequence, sequence)

    def test_float64_noise_that_packs_to_the_same_native_payload_is_suppressed(self):
        process = MemoryProcess()
        publisher = mailbox.MovementMailboxPublisher(process, process.base)
        publisher.publish(command(0x2000, 1, 0.5, 1.25))
        process.writes.clear()

        almost_identical = command(0x2000, 1, 0.5 + 1e-12, 1.25 + 1e-12)
        self.assertNotEqual(almost_identical, publisher.last_command)
        self.assertFalse(publisher.publish(almost_identical))
        self.assertEqual(process.writes, [])

    def test_generation_is_part_of_the_command_identity(self):
        process = MemoryProcess()
        publisher = mailbox.MovementMailboxPublisher(process, process.base)
        publisher.publish(command(0x2000, 1, 0.5, 1.25))
        process.writes.clear()

        self.assertTrue(publisher.publish(command(0x2000, 2, 0.5, 1.25)))
        self.assertEqual(
            [address - process.base for address, _data in process.writes],
            [
                mailbox.FALLBACK_SEQUENCE_OFFSET,
                mailbox.FALLBACK_ACTIVE_OFFSET,
                mailbox.FALLBACK_SEQUENCE_OFFSET,
                mailbox.SEQUENCE_OFFSET,
                mailbox.ACTIVE_OFFSET,
                mailbox.SEQUENCE_OFFSET,
            ],
        )

    def test_release_is_a_complete_committed_snapshot(self):
        process = MemoryProcess()
        publisher = mailbox.MovementMailboxPublisher(process, process.base)
        publisher.publish(command(0x2000, 1, 0.5, 1.25))

        publisher.publish(mailbox.MovementCommand.inactive(world_epoch=8))
        _sequence, released = mailbox.read_consistent(process, process.base)

        self.assertFalse(released.active)
        self.assertEqual(released.actor, 0)
        self.assertEqual(released.phys, 0)
        self.assertEqual(released.world_epoch, 8)

    def test_restart_reset_retires_the_existing_sequence_atomically(self):
        process = MemoryProcess()
        old_publisher = mailbox.MovementMailboxPublisher(process, process.base)
        old = command(0x2000, 1, 0.5, 1.25)
        old_publisher.publish(old)
        old_sequence = old_publisher.sequence
        observations = []

        def sample():
            result = mailbox.read_consistent(process, process.base, attempts=1)
            observations.append(None if result is None else result[1])

        process.after_write = sample
        replacement = mailbox.reset_mailbox(process, process.base)

        self.assertGreater(replacement.sequence, old_sequence)
        self.assertTrue(observations)
        self.assertTrue(
            all(item is None or item == old or not item.active for item in observations)
        )
        committed = mailbox.read_consistent(process, process.base)
        self.assertIsNotNone(committed)
        self.assertFalse(committed[1].active)


if __name__ == "__main__":
    unittest.main()
