from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

import camera_frame_sync as sync  # noqa: E402


class MemoryProcess:
    """Byte-addressable remote-memory fake with observable write boundaries."""

    def __init__(self, base: int = 0x10000000) -> None:
        self.base = int(base)
        self.memory = bytearray(sync.CAMERA_FRAME_STATE_SIZE)
        self.writes: list[tuple[int, bytes]] = []
        self.after_write = None
        self.after_read = None

    def _range(self, address: int, size: int) -> tuple[int, int]:
        start = int(address) - self.base
        end = start + int(size)
        if start < 0 or end > len(self.memory):
            raise OSError(f"camera test memory out of range: {address:#x}+{size:#x}")
        return start, end

    def put(self, address: int, data: bytes) -> None:
        payload = bytes(data)
        start, end = self._range(address, len(payload))
        self.memory[start:end] = payload

    def write(self, address: int, data: bytes) -> None:
        payload = bytes(data)
        self.put(address, payload)
        self.writes.append((int(address), payload))
        callback = self.after_write
        if callback is not None:
            callback()

    def read(self, address: int, size: int) -> bytes:
        start, end = self._range(address, size)
        result = bytes(self.memory[start:end])
        callback = self.after_read
        if callback is not None:
            callback(int(address), int(size))
        return result


class PresentClockProcess(MemoryProcess):
    def __init__(self, base: int = 0x20000000) -> None:
        super().__init__(base)
        self.memory = bytearray(0x3000)


def goal_signature(goal: sync.CameraGoal | None):
    if goal is None:
        return None
    return (
        goal.active,
        goal.reset_epoch,
        goal.producer_tick_us,
        goal.focus,
        goal.velocity,
        goal.extra_distance,
        goal.yaw_offset,
        goal.pitch_offset,
        goal.collision_exclusion,
    )


def publish_goal(
    publisher: sync.CameraGoalPublisher,
    process: MemoryProcess,
    marker: float,
    *,
    source_reset_epoch: int = 7,
    source_mode: int = 0,
    producer_tick_us: int = 10_000_000,
    velocity: tuple[float, float, float] | None = None,
) -> sync.CameraGoal:
    if velocity is None:
        velocity = (marker + 4.0, marker + 5.0, marker + 6.0)
    return publisher.publish(
        process,
        process.base,
        (marker + 1.0, marker + 2.0, marker + 3.0),
        velocity,
        marker + 7.0,
        marker + 8.0,
        marker + 9.0,
        (marker + 10.0, marker + 11.0),
        source_reset_epoch=source_reset_epoch,
        source_mode=source_mode,
        producer_tick_us=producer_tick_us,
    )


def camera_goal(
    *,
    focus=(0.0, 0.0, 0.0),
    velocity=(0.0, 0.0, 0.0),
    extra_distance=0.0,
    reset_epoch=1,
    producer_tick_us=10_000_000,
) -> sync.CameraGoal:
    return sync.CameraGoal(
        True,
        int(reset_epoch),
        int(producer_tick_us),
        tuple(float(value) for value in focus),
        tuple(float(value) for value in velocity),
        float(extra_distance),
        0.0,
        0.0,
        (0.0, 0.0),
    )


class CameraGoalDoubleBufferTests(unittest.TestCase):
    def test_renderer_output_uses_an_atomic_commit_and_complete_float_snapshot(self):
        self.assertEqual(sync.CAMERA_OUTPUT_COMMIT_OFFSET, 0xA8)
        self.assertEqual(sync.CAMERA_OUTPUT_ACTIVE_OFFSET, 0xAC)
        self.assertEqual(sync.CAMERA_OUTPUT_SLOT_OFFSETS, (0xB0, 0xD0))
        self.assertEqual(sync.CAMERA_OUTPUT_PAYLOAD.format, "<8f")
        self.assertEqual(sync.CAMERA_OUTPUT_PAYLOAD.size, 0x20)

    def test_first_publication_is_unavailable_until_the_generation_commit(self):
        process = MemoryProcess()
        publisher = sync.CameraGoalPublisher()
        observations = []
        process.after_write = lambda: observations.append(
            goal_signature(sync.read_committed_goal(process, process.base))
        )

        published = publish_goal(publisher, process, 1.0)

        self.assertEqual(
            observations,
            [None, None, goal_signature(published)],
        )
        self.assertEqual(
            [address - process.base for address, _payload in process.writes],
            [
                sync.CAMERA_GOAL_HEADER_OFFSET,
                sync.CAMERA_GOAL_SLOT_OFFSETS[1],
                sync.CAMERA_GOAL_GENERATION_OFFSET,
            ],
        )

    def test_replacement_exposes_only_the_old_or_new_complete_goal(self):
        process = MemoryProcess()
        publisher = sync.CameraGoalPublisher()
        old = publish_goal(publisher, process, 1.0)
        observations = []
        process.writes.clear()
        process.after_write = lambda: observations.append(
            goal_signature(sync.read_committed_goal(process, process.base))
        )

        new = publish_goal(publisher, process, 20.0, producer_tick_us=10_012_000)

        self.assertEqual(
            observations,
            [goal_signature(old), goal_signature(new)],
        )
        self.assertEqual(
            [address - process.base for address, _payload in process.writes],
            [
                sync.CAMERA_GOAL_SLOT_OFFSETS[0],
                sync.CAMERA_GOAL_GENERATION_OFFSET,
            ],
        )
        # Every coupled field changes. Seeing either exact signature proves a
        # reader cannot combine a new focus with old velocity/zoom/exclusion.
        self.assertNotEqual(old.focus, new.focus)
        self.assertNotEqual(old.velocity, new.velocity)
        self.assertNotEqual(old.collision_exclusion, new.collision_exclusion)

    def test_present_clock_uses_the_generation_selected_tick_slot(self):
        process = PresentClockProcess()
        state = process.base + sync.CAMERA_FRAME_STATE_OFFSET
        generation = 17
        tick_us = 9_876_543_210
        process.put(
            process.base + sync.CAMERA_PRESENT_GENERATION_CAVE_OFFSET,
            struct.pack("<I", generation),
        )
        process.put(
            state
            + sync.generation_slot(
                generation,
                sync.CAMERA_PRESENT_TICK_SLOT_OFFSETS,
            ),
            struct.pack("<Q", tick_us),
        )

        sample = sync.read_present_sample(process, process.base, state)

        self.assertEqual(sample, sync.CameraPresentSample(generation, tick_us))

    def test_present_clock_rejects_a_generation_change_during_tick_read(self):
        process = PresentClockProcess()
        state = process.base + sync.CAMERA_FRAME_STATE_OFFSET
        generation = 22
        slot = state + sync.generation_slot(
            generation,
            sync.CAMERA_PRESENT_TICK_SLOT_OFFSETS,
        )
        process.put(
            process.base + sync.CAMERA_PRESENT_GENERATION_CAVE_OFFSET,
            struct.pack("<I", generation),
        )
        process.put(slot, struct.pack("<Q", 123_456_789))

        def tear_after_tick(address: int, size: int) -> None:
            if address != slot or size != 8:
                return
            process.after_read = None
            process.put(
                process.base + sync.CAMERA_PRESENT_GENERATION_CAVE_OFFSET,
                struct.pack("<I", generation + 1),
            )

        process.after_read = tear_after_tick

        self.assertIsNone(sync.read_present_sample(process, process.base, state))

    def test_reader_rejects_a_generation_that_changes_during_the_read(self):
        process = MemoryProcess()
        publisher = sync.CameraGoalPublisher()
        publish_goal(publisher, process, 1.0)
        committed = publisher.generation
        slot_address = process.base + sync.generation_slot(
            committed,
            sync.CAMERA_GOAL_SLOT_OFFSETS,
        )

        def tear_after_payload(address: int, size: int) -> None:
            if address != slot_address or size != sync.CAMERA_GOAL_SLOT_SIZE:
                return
            process.after_read = None
            process.put(
                process.base + sync.CAMERA_GOAL_GENERATION_OFFSET,
                struct.pack("<I", sync.next_u32_generation(committed)),
            )

        process.after_read = tear_after_payload

        self.assertIsNone(sync.read_committed_goal(process, process.base))

    def test_generation_wrap_keeps_the_next_write_in_the_inactive_slot(self):
        process = MemoryProcess()
        publisher = sync.CameraGoalPublisher()
        publish_goal(publisher, process, 0.0)

        # Materialize the final even/odd pair before u32 wrap.
        publisher.generation = 0xFFFFFFFD
        publish_goal(publisher, process, 10.0, producer_tick_us=10_010_000)
        previous = publish_goal(publisher, process, 20.0, producer_tick_us=10_020_000)
        self.assertEqual(publisher.generation, 0xFFFFFFFF)

        wrapped_generation = sync.next_u32_generation(publisher.generation)
        self.assertNotEqual(wrapped_generation, 0)
        self.assertNotEqual(
            sync.generation_slot(publisher.generation, sync.CAMERA_GOAL_SLOT_OFFSETS),
            sync.generation_slot(wrapped_generation, sync.CAMERA_GOAL_SLOT_OFFSETS),
            "u32 wrap must not turn the committed slot into the writer slot",
        )

        observations = []
        process.after_write = lambda: observations.append(
            goal_signature(sync.read_committed_goal(process, process.base))
        )
        current = publish_goal(publisher, process, 30.0, producer_tick_us=10_030_000)

        self.assertEqual(
            observations,
            [goal_signature(previous), goal_signature(current)],
        )


class CameraGoalResetTests(unittest.TestCase):
    def test_source_reset_epoch_changes_only_on_a_source_edge(self):
        process = MemoryProcess()
        publisher = sync.CameraGoalPublisher()

        first = publish_goal(publisher, process, 1.0, source_reset_epoch=41)
        stable = publish_goal(publisher, process, 2.0, source_reset_epoch=41)
        changed = publish_goal(publisher, process, 3.0, source_reset_epoch=42)
        stable_again = publish_goal(publisher, process, 4.0, source_reset_epoch=42)

        self.assertEqual(first.reset_epoch, stable.reset_epoch)
        self.assertNotEqual(stable.reset_epoch, changed.reset_epoch)
        self.assertEqual(changed.reset_epoch, stable_again.reset_epoch)

    def test_external_source_mode_edges_reset_on_entry_and_exit(self):
        process = MemoryProcess()
        publisher = sync.CameraGoalPublisher()

        live = publish_goal(publisher, process, 1.0, source_mode=0)
        live_again = publish_goal(publisher, process, 2.0, source_mode=0)
        external = publish_goal(
            publisher,
            process,
            3.0,
            source_mode=1,
            velocity=(0.0, 0.0, 0.0),
        )
        external_again = publish_goal(
            publisher,
            process,
            4.0,
            source_mode=1,
            velocity=(0.0, 0.0, 0.0),
        )
        resumed = publish_goal(publisher, process, 5.0, source_mode=0)

        self.assertEqual(live.reset_epoch, live_again.reset_epoch)
        self.assertNotEqual(live_again.reset_epoch, external.reset_epoch)
        self.assertEqual(external.reset_epoch, external_again.reset_epoch)
        self.assertNotEqual(external_again.reset_epoch, resumed.reset_epoch)
        self.assertEqual(external.velocity, (0.0, 0.0, 0.0))

    def test_new_reset_epoch_snaps_instead_of_flying_in(self):
        resampler = sync.CameraFrameResampler()
        start = camera_goal(focus=(0.0, 0.0, 0.0), reset_epoch=1)
        same_epoch = camera_goal(
            focus=(30.0, 0.0, 0.0),
            reset_epoch=1,
            producer_tick_us=10_010_000,
        )
        new_epoch = camera_goal(
            focus=(80.0, 4.0, -6.0),
            reset_epoch=2,
            producer_tick_us=10_020_000,
        )

        resampler.advance(start, 1, 10_000_000)
        blended = resampler.advance(same_epoch, 2, 10_010_000)
        snapped = resampler.advance(new_epoch, 3, 10_020_000)

        self.assertGreater(blended.focus[0], 0.0)
        self.assertLess(blended.focus[0], 30.0)
        self.assertEqual(snapped.focus, new_epoch.focus)
        self.assertEqual(snapped.reset_epoch, 2)


class CameraFrameResamplerContractTests(unittest.TestCase):
    def test_duplicate_present_is_idempotent(self):
        goal = camera_goal(
            focus=(1.0, 2.0, 3.0),
            velocity=(6.0, 0.0, -2.0),
            producer_tick_us=20_000_000,
        )
        with_duplicate = sync.CameraFrameResampler()
        reference = sync.CameraFrameResampler()

        first = with_duplicate.advance(goal, 100, 20_000_000)
        duplicate = with_duplicate.advance(goal, 100, 20_000_000)
        expected_first = reference.advance(goal, 100, 20_000_000)

        self.assertTrue(first.advanced)
        self.assertFalse(duplicate.advanced)
        self.assertEqual(duplicate.focus, first.focus)
        self.assertEqual(first.focus, expected_first.focus)

        actual_next = with_duplicate.advance(goal, 101, 20_016_667)
        expected_next = reference.advance(goal, 101, 20_016_667)
        self.assertEqual(actual_next.focus, expected_next.focus)

    def test_skipped_present_generations_advance_only_once(self):
        goal = camera_goal(
            focus=(0.0, 0.0, 0.0),
            velocity=(4.0, 0.0, 0.0),
            producer_tick_us=30_000_000,
        )
        skipped = sync.CameraFrameResampler()
        adjacent = sync.CameraFrameResampler()
        skipped.advance(goal, 7, 30_000_000)
        adjacent.advance(goal, 7, 30_000_000)

        jumped = skipped.advance(goal, 700, 30_016_667)
        ordinary = adjacent.advance(goal, 8, 30_016_667)
        duplicate = skipped.advance(goal, 700, 30_016_667)

        self.assertTrue(jumped.advanced)
        self.assertFalse(duplicate.advanced)
        self.assertEqual(jumped.focus, ordinary.focus)
        self.assertEqual(duplicate.focus, jumped.focus)

    def test_present_generation_wrap_is_one_new_frame(self):
        goal = camera_goal(
            focus=(0.0, 0.0, 0.0),
            velocity=(1.0, 0.0, 0.0),
            producer_tick_us=40_000_000,
        )
        resampler = sync.CameraFrameResampler()

        before = resampler.advance(goal, 0xFFFFFFFF, 40_000_000)
        wrapped = resampler.advance(goal, 0, 40_016_667)
        duplicate = resampler.advance(goal, 0, 40_016_667)

        self.assertTrue(before.advanced)
        self.assertTrue(wrapped.advanced)
        self.assertFalse(duplicate.advanced)
        self.assertGreater(wrapped.focus[0], before.focus[0])

    def test_stale_goal_fails_inactive_and_a_fresh_goal_snaps_back(self):
        goal = camera_goal(
            focus=(5.0, 6.0, 7.0),
            velocity=(3.0, 0.0, 0.0),
            producer_tick_us=50_000_000,
        )
        resampler = sync.CameraFrameResampler()

        boundary = resampler.advance(
            goal,
            1,
            50_000_000 + int(sync.CAMERA_BRIDGE_STALE_SECONDS * 1_000_000.0),
        )
        stale = resampler.advance(
            goal,
            2,
            50_000_001 + int(sync.CAMERA_BRIDGE_STALE_SECONDS * 1_000_000.0),
        )
        stale_duplicate = resampler.advance(goal, 2, stale.present_tick_us)
        fresh_goal = camera_goal(
            focus=(20.0, -1.0, 2.0),
            velocity=(0.0, 0.0, 0.0),
            reset_epoch=goal.reset_epoch,
            producer_tick_us=stale.present_tick_us + 1,
        )
        recovered = resampler.advance(fresh_goal, 3, stale.present_tick_us + 1)

        self.assertTrue(boundary.active)
        self.assertFalse(stale.active)
        self.assertTrue(stale.advanced)
        self.assertFalse(stale_duplicate.advanced)
        self.assertTrue(recovered.active)
        self.assertEqual(recovered.focus, fresh_goal.focus)

    def test_prediction_is_capped_and_old_velocity_cannot_drift_forever(self):
        velocity = (10.0, -4.0, 2.0)
        goal = camera_goal(
            focus=(2.0, 3.0, 4.0),
            velocity=velocity,
            producer_tick_us=60_000_000,
        )
        horizon_us = int(
            sync.CAMERA_BRIDGE_PREDICTION_HORIZON_SECONDS * 1_000_000.0
        )
        desired = tuple(
            goal.focus[index]
            + velocity[index] * sync.CAMERA_BRIDGE_PREDICTION_HORIZON_SECONDS
            for index in range(3)
        )
        resampler = sync.CameraFrameResampler()

        at_horizon = resampler.advance(goal, 1, 60_000_000 + horizon_us)
        after_horizon = resampler.advance(goal, 2, 60_000_000 + horizon_us + 30_000)
        much_later = resampler.advance(goal, 3, 60_000_000 + horizon_us + 130_000)

        self.assertEqual(at_horizon.focus, desired)
        self.assertEqual(after_horizon.focus, desired)
        self.assertEqual(much_later.focus, desired)

    def test_negative_goal_age_never_predicts_backwards(self):
        goal = camera_goal(
            focus=(8.0, 1.0, -3.0),
            velocity=(100.0, 100.0, 100.0),
            producer_tick_us=70_010_000,
        )

        frame = sync.CameraFrameResampler().advance(goal, 1, 70_000_000)

        self.assertEqual(frame.focus, goal.focus)

    def test_zoom_is_resampled_on_every_present_instead_of_stepping(self):
        resampler = sync.CameraFrameResampler()
        start = camera_goal(extra_distance=0.0, producer_tick_us=80_000_000)
        changed = camera_goal(extra_distance=12.0, producer_tick_us=80_010_000)

        first = resampler.advance(start, 1, 80_000_000)
        blended = resampler.advance(changed, 2, 80_016_667)
        duplicate = resampler.advance(changed, 2, 80_016_667)
        continued = resampler.advance(changed, 3, 80_033_334)

        self.assertEqual(first.extra_distance, 0.0)
        self.assertGreater(blended.extra_distance, 0.0)
        self.assertLess(blended.extra_distance, changed.extra_distance)
        self.assertEqual(duplicate.extra_distance, blended.extra_distance)
        self.assertGreater(continued.extra_distance, blended.extra_distance)
        self.assertLess(continued.extra_distance, changed.extra_distance)


JITTER_MS = (0.0, 1.75, -1.25, 0.75, -1.75, 1.0, -0.5, 0.0)


def producer_schedule(rate_hz: int, duration_ms: int) -> tuple[int, ...]:
    ticks = []
    index = 0
    while True:
        tick = int(round(index * 1000.0 / rate_hz + JITTER_MS[index % len(JITTER_MS)]))
        if ticks and tick <= ticks[-1]:
            tick = ticks[-1] + 1
        if tick > duration_ms:
            break
        ticks.append(tick)
        index += 1
    return tuple(ticks)


def present_schedule(rate_hz: int, duration_ms: int) -> tuple[int, ...]:
    ticks = []
    frame = 0
    while True:
        tick = int(round(frame * 1000.0 / rate_hz))
        if tick > duration_ms:
            break
        if not ticks or tick != ticks[-1]:
            ticks.append(tick)
        frame += 1
    return tuple(ticks)


def simulate_linear_camera(
    producer_hz: int,
    present_hz: int,
    *,
    duration_ms: int = 4_000,
    speed: float = 6.0,
):
    producer_ticks = producer_schedule(producer_hz, duration_ms)
    present_ticks = present_schedule(present_hz, duration_ms)
    producer_index = 0
    current_producer_tick = producer_ticks[0]
    base_tick_us = 1_000_000_000
    resampler = sync.CameraFrameResampler()
    generation = 0
    maximum_error = 0.0
    advanced_count = 0
    samples = []

    for present_tick in present_ticks:
        while (
            producer_index + 1 < len(producer_ticks)
            and producer_ticks[producer_index + 1] <= present_tick
        ):
            producer_index += 1
            current_producer_tick = producer_ticks[producer_index]
        producer_seconds = current_producer_tick / 1000.0
        goal = camera_goal(
            focus=(speed * producer_seconds, 2.0, -3.0),
            velocity=(speed, 0.0, 0.0),
            producer_tick_us=base_tick_us + current_producer_tick * 1000,
        )
        generation = sync.next_u32_generation(generation)
        frame = resampler.advance(
            goal,
            generation,
            base_tick_us + present_tick * 1000,
        )
        expected = speed * present_tick / 1000.0
        maximum_error = max(maximum_error, abs(frame.focus[0] - expected))
        advanced_count += int(frame.advanced)
        samples.append((present_tick, frame.focus))

    return {
        "producer_ticks": producer_ticks,
        "present_ticks": present_ticks,
        "advanced_count": advanced_count,
        "maximum_error": maximum_error,
        "samples": tuple(samples),
    }


class CameraCadenceModelTests(unittest.TestCase):
    def test_jittered_84_and_100hz_goals_resample_cleanly_at_all_frame_rates(self):
        for producer_hz in (84, 100):
            for present_hz in (30, 60, 120, 240):
                with self.subTest(producer_hz=producer_hz, present_hz=present_hz):
                    result = simulate_linear_camera(producer_hz, present_hz)
                    self.assertEqual(
                        result["advanced_count"],
                        len(result["present_ticks"]),
                    )
                    self.assertLess(result["maximum_error"], 1.0e-9)
                    horizontal = [focus[0] for _tick, focus in result["samples"]]
                    self.assertTrue(
                        all(
                            later >= earlier
                            for earlier, later in zip(horizontal, horizontal[1:])
                        )
                    )

    def test_input_poll_cadence_is_independent_from_present_cadence(self):
        for producer_hz in (84, 100):
            results = {
                present_hz: simulate_linear_camera(producer_hz, present_hz)
                for present_hz in (30, 60, 120, 240)
            }
            expected_input_ticks = results[30]["producer_ticks"]
            for present_hz, result in results.items():
                with self.subTest(producer_hz=producer_hz, present_hz=present_hz):
                    self.assertEqual(result["producer_ticks"], expected_input_ticks)
                    self.assertEqual(
                        result["advanced_count"],
                        len(result["present_ticks"]),
                    )

            # At high FPS the camera advances more often than new input/goals.
            # This is the contract that a 10-12 ms producer cannot provide by
            # itself and proves the two cadences remain intentionally separate.
            self.assertGreater(
                results[240]["advanced_count"],
                len(expected_input_ticks),
            )

    def test_microsecond_clock_is_exact_at_fractional_and_high_frame_rates(self):
        speed = 6.0
        duration_us = 2_000_000
        base_tick_us = 3_000_000_000
        for present_hz in (59.94, 100.0, 144.0, 240.0, 360.0):
            with self.subTest(present_hz=present_hz):
                resampler = sync.CameraFrameResampler()
                producer_index = 0
                generation = 0
                maximum_error = 0.0
                frame = 0
                while True:
                    present_us = round(frame * 1_000_000.0 / present_hz)
                    if present_us > duration_us:
                        break
                    while round(
                        (producer_index + 1) * 1_000_000.0 / 100.0
                    ) <= present_us:
                        producer_index += 1
                    producer_us = round(producer_index * 1_000_000.0 / 100.0)
                    goal = camera_goal(
                        focus=(speed * producer_us / 1_000_000.0, 0.0, 0.0),
                        velocity=(speed, 0.0, 0.0),
                        producer_tick_us=base_tick_us + producer_us,
                    )
                    generation = sync.next_u32_generation(generation)
                    output = resampler.advance(
                        goal,
                        generation,
                        base_tick_us + present_us,
                    )
                    expected = speed * present_us / 1_000_000.0
                    maximum_error = max(
                        maximum_error,
                        abs(output.focus[0] - expected),
                    )
                    frame += 1
                self.assertLess(maximum_error, 1.0e-9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
