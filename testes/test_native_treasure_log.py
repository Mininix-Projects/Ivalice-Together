from __future__ import annotations

import unittest

from native_treasure_log import (
    TREASURE_ACTOR_ACTION_CALL_SITE,
    TREASURE_ACTOR_ACTION_ORIGINAL,
    TREASURE_COMBAT_LOG_MODE,
    TREASURE_INTERACTION_STATE,
    TREASURE_MODAL_MODE,
    TREASURE_NO_ACTOR_CAVE_OFFSET,
    TREASURE_PRESENTATION_MODE_SITE,
    TREASURE_SOUND_CALL_ORIGINAL,
    TREASURE_SOUND_CALL_SITE,
    TREASURE_SOUND_CAVE_OFFSET,
    TREASURE_SOUND_ID,
    TREASURE_SOUND_STATE_OFFSET,
    NativeTreasureLog,
    build_no_actor_wrapper,
    build_sound_wrapper,
)

CAVE_BASE = 0x61000000
TREASURE_MESSAGE_POLL_CALL_SITE = 0x0050FBC6
TREASURE_MESSAGE_POLL_CALL_ORIGINAL = bytes.fromhex("E8 55 E3 E4 FF")


class FakeProcess:
    def __init__(self, *, presentation: bytes = TREASURE_MODAL_MODE) -> None:
        self.memory = {
            TREASURE_PRESENTATION_MODE_SITE: presentation,
            TREASURE_ACTOR_ACTION_CALL_SITE: TREASURE_ACTOR_ACTION_ORIGINAL,
            TREASURE_MESSAGE_POLL_CALL_SITE: TREASURE_MESSAGE_POLL_CALL_ORIGINAL,
            TREASURE_SOUND_CALL_SITE: TREASURE_SOUND_CALL_ORIGINAL,
        }
        self.writes: list[tuple[str, int, bytes]] = []

    def read(self, address: int, size: int) -> bytes:
        payload = self.memory.get(address, b"\x00" * size)
        return payload[:size].ljust(size, b"\x00")

    def write(self, address: int, payload: bytes) -> None:
        self.memory[address] = bytes(payload)
        self.writes.append(("write", address, bytes(payload)))

    def patch_code(self, address: int, payload: bytes) -> None:
        self.memory[address] = bytes(payload)
        self.writes.append(("patch", address, bytes(payload)))


class NativeTreasureLogTests(unittest.TestCase):
    def test_install_restores_instant_native_hide_without_actor_lock(self):
        process = FakeProcess()
        bridge = NativeTreasureLog(process, CAVE_BASE)

        bridge.install()

        self.assertTrue(bridge.active)
        self.assertTrue(bridge.owned)
        self.assertEqual(len(process.writes), 5)
        self.assertEqual(
            process.memory[TREASURE_PRESENTATION_MODE_SITE],
            TREASURE_COMBAT_LOG_MODE,
        )
        self.assertEqual(
            process.memory[CAVE_BASE + TREASURE_NO_ACTOR_CAVE_OFFSET],
            build_no_actor_wrapper(CAVE_BASE + TREASURE_NO_ACTOR_CAVE_OFFSET),
        )
        self.assertNotEqual(
            process.memory[TREASURE_ACTOR_ACTION_CALL_SITE],
            TREASURE_ACTOR_ACTION_ORIGINAL,
        )
        self.assertEqual(
            process.memory[TREASURE_MESSAGE_POLL_CALL_SITE],
            TREASURE_MESSAGE_POLL_CALL_ORIGINAL,
        )

    def test_restore_disarms_state_without_unpatching_process_lifetime_code(self):
        process = FakeProcess()
        bridge = NativeTreasureLog(process, CAVE_BASE)
        bridge.install()
        roots = {
            address: process.memory[address]
            for address in (
                TREASURE_PRESENTATION_MODE_SITE,
                TREASURE_ACTOR_ACTION_CALL_SITE,
                TREASURE_SOUND_CALL_SITE,
            )
        }
        process.writes.clear()

        bridge.restore()

        self.assertEqual(
            {address: process.memory[address] for address in roots},
            roots,
        )
        self.assertEqual(
            process.writes,
            [("write", CAVE_BASE + TREASURE_SOUND_STATE_OFFSET, b"\x00" * 4)],
        )
        self.assertFalse(bridge.active)

    def test_preinstalled_compatible_code_is_idempotent_and_not_claimed(self):
        process = FakeProcess(presentation=TREASURE_COMBAT_LOG_MODE)
        bridge = NativeTreasureLog(process, CAVE_BASE)
        actor_wrapper = build_no_actor_wrapper(
            CAVE_BASE + TREASURE_NO_ACTOR_CAVE_OFFSET
        )
        sound_wrapper = build_sound_wrapper(
            CAVE_BASE + TREASURE_SOUND_CAVE_OFFSET,
            CAVE_BASE + TREASURE_SOUND_STATE_OFFSET,
        )
        process.memory[CAVE_BASE + TREASURE_NO_ACTOR_CAVE_OFFSET] = actor_wrapper
        process.memory[TREASURE_ACTOR_ACTION_CALL_SITE] = bridge._actor_patch
        process.memory[CAVE_BASE + TREASURE_SOUND_CAVE_OFFSET] = sound_wrapper
        process.memory[TREASURE_SOUND_CALL_SITE] = bridge._sound_patch

        bridge.install()
        process.writes.clear()
        bridge.restore()

        self.assertFalse(bridge.owned)
        self.assertEqual(
            process.writes,
            [("write", CAVE_BASE + TREASURE_SOUND_STATE_OFFSET, b"\x00" * 4)],
        )

    def test_unknown_presentation_code_is_never_overwritten(self):
        process = FakeProcess(presentation=b"\xcc" * len(TREASURE_MODAL_MODE))

        with self.assertRaises(RuntimeError):
            NativeTreasureLog(process, CAVE_BASE).install()

        self.assertEqual(process.writes, [])

    def test_unknown_actor_action_code_is_preserved_during_rollback(self):
        process = FakeProcess()
        process.memory[TREASURE_ACTOR_ACTION_CALL_SITE] = b"\xcc" * 5

        with self.assertRaises(RuntimeError):
            NativeTreasureLog(process, CAVE_BASE).install()

        self.assertEqual(
            process.memory[TREASURE_PRESENTATION_MODE_SITE], TREASURE_MODAL_MODE
        )
        self.assertEqual(process.memory[TREASURE_ACTOR_ACTION_CALL_SITE], b"\xcc" * 5)
        self.assertEqual(process.writes, [])

    def test_partial_live_install_aborts_without_hot_repair(self):
        process = FakeProcess(presentation=TREASURE_COMBAT_LOG_MODE)
        memory_before = dict(process.memory)

        with self.assertRaisesRegex(RuntimeError, "partial native treasure-log"):
            NativeTreasureLog(process, CAVE_BASE).install()

        self.assertEqual(process.memory, memory_before)
        self.assertEqual(process.writes, [])

    def test_stock_roots_with_old_cave_abort_without_overwrite(self):
        process = FakeProcess()
        cave = CAVE_BASE + TREASURE_NO_ACTOR_CAVE_OFFSET
        process.memory[cave] = b"old reachable code"
        memory_before = dict(process.memory)

        with self.assertRaisesRegex(RuntimeError, "non-empty treasure no-actor"):
            NativeTreasureLog(process, CAVE_BASE).install()

        self.assertEqual(process.memory, memory_before)
        self.assertEqual(process.writes, [])

    def test_live_wrapper_mismatch_aborts_without_any_mutation(self):
        process = FakeProcess(presentation=TREASURE_COMBAT_LOG_MODE)
        bridge = NativeTreasureLog(process, CAVE_BASE)
        actor_wrapper = build_no_actor_wrapper(
            CAVE_BASE + TREASURE_NO_ACTOR_CAVE_OFFSET
        )
        sound_wrapper = build_sound_wrapper(
            CAVE_BASE + TREASURE_SOUND_CAVE_OFFSET,
            CAVE_BASE + TREASURE_SOUND_STATE_OFFSET,
        )
        process.memory[CAVE_BASE + TREASURE_NO_ACTOR_CAVE_OFFSET] = (
            bytes((actor_wrapper[0] ^ 1,)) + actor_wrapper[1:]
        )
        process.memory[TREASURE_ACTOR_ACTION_CALL_SITE] = bridge._actor_patch
        process.memory[CAVE_BASE + TREASURE_SOUND_CAVE_OFFSET] = sound_wrapper
        process.memory[TREASURE_SOUND_CALL_SITE] = bridge._sound_patch
        memory_before = dict(process.memory)

        with self.assertRaisesRegex(RuntimeError, "wrapper is not intact"):
            NativeTreasureLog(process, CAVE_BASE).install()

        self.assertEqual(process.memory, memory_before)
        self.assertEqual(process.writes, [])

    def test_wrapper_only_publishes_finished_interaction_state(self):
        wrapper = build_no_actor_wrapper(CAVE_BASE + TREASURE_NO_ACTOR_CAVE_OFFSET)
        self.assertIn(TREASURE_INTERACTION_STATE.to_bytes(8, "little"), wrapper)
        self.assertNotIn((0x201C).to_bytes(4, "little"), wrapper)
        self.assertEqual(wrapper[-1], 0xC3)

    def test_sound_wrapper_publishes_the_traced_native_reward_sound(self):
        wrapper = build_sound_wrapper(
            CAVE_BASE + TREASURE_SOUND_CAVE_OFFSET,
            CAVE_BASE + TREASURE_SOUND_STATE_OFFSET,
        )

        self.assertIn(
            (CAVE_BASE + TREASURE_SOUND_STATE_OFFSET).to_bytes(8, "little"),
            wrapper,
        )
        self.assertIn(TREASURE_SOUND_ID.to_bytes(4, "little"), wrapper)


if __name__ == "__main__":
    unittest.main(verbosity=2)
