from __future__ import annotations

import struct
import unittest

import coop_runtime as coop


class FakeProcess:
    def __init__(self):
        self.writes = {}
        self.patches = {}

    def read(self, address, size):
        if address == coop.FOLLOWER_LIST_EVALUATOR_SITE:
            return b"\x48\x89\x9c\x24\x80\x00\x00\x00"[:size]
        if address == coop.ACTOR_PHYSICS_HELPER:
            return b"\x00" * size
        raise AssertionError(f"unexpected read at {address:#x}")

    def write(self, address, data):
        self.writes[address] = bytes(data)

    def patch_code(self, address, data):
        self.patches[address] = bytes(data)


class FakeActorProcess:
    def __init__(
        self,
        actor,
        *,
        state=1,
        target=0,
        action=0x4124,
        descriptor_action=0x4124,
    ):
        self.actor = actor
        self.memory = {}
        descriptor = actor + 0x1000
        self.write(actor + 0x6B4, bytes((state,)))
        self.write(actor + 0x708, struct.pack("<Q", descriptor))
        self.write(actor + 0x710, struct.pack("<I", target))
        self.write(actor + 0x714, struct.pack("<H", action))
        self.write(descriptor, struct.pack("<H", descriptor_action))
        self.write(actor + 0x6E8, struct.pack("<Q", coop.FOLLOWER_LIST_SECONDARY))
        self.write(actor + 0x6F0, struct.pack("<Q", coop.FOLLOWER_LIST_PRIMARY))

    def read(self, address, size):
        return bytes(self.memory.get(address + offset, 0) for offset in range(size))

    def write(self, address, data):
        for offset, value in enumerate(data):
            self.memory[address + offset] = value

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]


class FollowerListGuardTests(unittest.TestCase):
    def test_guard_rejects_follow_lists_only_for_published_p2(self):
        if coop.FOLLOWER_LIST_GUARD_REJECTS_EVERY_LIST:
            self.skipTest(
                "test switch is rejecting every list, not just the follow ones"
            )
        process = FakeProcess()
        coop.install_follower_list_guard(process)

        cave = process.writes[coop.FOLLOWER_LIST_GUARD_CAVE]
        self.assertIn(
            b"\x49\xbb" + struct.pack("<Q", coop.FOLLOWER_LIST_PRIMARY),
            cave,
        )
        self.assertIn(
            b"\x49\xbb" + struct.pack("<Q", coop.FOLLOWER_LIST_SECONDARY),
            cave,
        )
        self.assertIn(
            b"\x41\x83\x7b" + bytes((coop.COOP_ACTIVE_OFFSET,)) + b"\x00",
            cave,
            "Follower AI must stay suppressed for the whole co-op session",
        )
        self.assertNotIn(
            b"\x41\x83\x7b" + bytes((coop.MOVEMENT_ACTIVE_OFFSET,)) + b"\x00",
            cave,
            "Opening a menu/modal must not briefly re-enable follow-to-leader",
        )
        self.assertIn(
            b"\x4d\x3b\x6b\x10",
            cave,
            "Released characters must retain native follower-list evaluation",
        )
        self.assertEqual(
            process.patches[coop.FOLLOWER_LIST_EVALUATOR_SITE][-3:],
            b"\x90\x90\x90",
        )

    def test_sanitize_publishes_without_writing_actor_action(self):
        actor = 0x71000000
        process = FakeActorProcess(actor)
        coop.set_dual_interaction_base(0x51000000)
        self.assertFalse(
            coop.sanitize_residual_follower_action(
                process,
                actor,
                True,
                role="p2",
                handle=0x00100031,
                expected_phys=actor + 0x2000,
            )
        )
        self.assertEqual(process.read(actor + 0x714, 2), struct.pack("<H", 0x4124))
        self.assertEqual(
            process.read(coop.P2_FOLLOWER_ACTION_MAILBOX, 4),
            struct.pack("<I", 2),
        )

    def test_invalid_publication_does_not_touch_real_action_or_targeted_state(self):
        actor = 0x71000000
        real_action = FakeActorProcess(actor, action=0x5001)
        self.assertFalse(
            coop.sanitize_residual_follower_action(
                real_action,
                actor,
                True,
                role="p2",
                handle=0,
                expected_phys=actor + 0x2000,
            )
        )
        targeted = FakeActorProcess(actor, target=0x10000F)
        self.assertFalse(
            coop.sanitize_residual_follower_action(
                targeted,
                actor,
                True,
                role="p2",
                handle=0,
                expected_phys=actor + 0x2000,
            )
        )
        self.assertEqual(real_action.read(actor + 0x714, 2), struct.pack("<H", 0x5001))
        self.assertEqual(targeted.u32(actor + 0x710), 0x10000F)

    def test_resume_is_a_one_shot_native_mailbox_request(self):
        actor = 0x71000000
        process = FakeActorProcess(actor, action=coop.NATIVE_IDLE_ACTION)
        coop.set_dual_interaction_base(0x51000000)

        self.assertFalse(
            coop.resume_sanitized_follower_action(
                process,
                actor,
                role="p3",
                handle=0x00100031,
                expected_phys=actor + 0x2000,
            )
        )
        self.assertEqual(
            process.read(actor + 0x714, 2),
            struct.pack("<H", coop.NATIVE_IDLE_ACTION),
        )
        self.assertEqual(
            process.read(coop.P3_FOLLOWER_ACTION_MAILBOX, 4),
            struct.pack("<I", 2),
        )

    def test_does_not_promote_a_genuine_native_idle(self):
        actor = 0x71000000
        process = FakeActorProcess(
            actor,
            action=coop.NATIVE_IDLE_ACTION,
            descriptor_action=coop.NATIVE_IDLE_ACTION,
        )

        self.assertFalse(
            coop.resume_sanitized_follower_action(
                process,
                actor,
                role="p3",
                handle=0,
                expected_phys=actor + 0x2000,
            )
        )
        self.assertEqual(
            process.read(actor + 0x714, 2),
            struct.pack("<H", coop.NATIVE_IDLE_ACTION),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
