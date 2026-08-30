import struct
import unittest

import coop_runtime as coop
import session_state


class FakeProcess:
    def __init__(self):
        self.memory = {}

    def read(self, address, size):
        return bytes(self.memory.get(address + index, 0) for index in range(size))

    def write(self, address, payload):
        for index, value in enumerate(bytes(payload)):
            self.memory[address + index] = value

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def put_u32(self, address, value):
        self.write(address, struct.pack("<I", value))

    def put_u64(self, address, value):
        self.write(address, struct.pack("<Q", value))


class FollowerGatewayStallDetectorTests(unittest.TestCase):
    def test_continuous_progress_never_triggers(self):
        detector = coop.FollowerGatewayStallDetector(
            progress_distance=0.1,
            stall_seconds=0.25,
        )
        for index in range(8):
            self.assertFalse(
                detector.observe(
                    2,
                    (index * 0.11, 0.0, 0.0),
                    0.1,
                    True,
                    index * 0.2,
                )
            )

    def test_stall_triggers_once_until_input_is_released(self):
        detector = coop.FollowerGatewayStallDetector(
            progress_distance=0.1,
            stall_seconds=0.25,
        )
        self.assertFalse(detector.observe(2, (3.0, 0.0, 7.0), 0.1, True, 0.0))
        self.assertTrue(detector.observe(2, (3.01, 0.0, 7.0), 0.1, True, 0.26))
        self.assertFalse(detector.observe(2, (3.01, 0.0, 7.0), 0.1, True, 1.0))
        self.assertFalse(detector.observe(2, (3.01, 0.0, 7.0), 0.0, True, 1.1))
        self.assertFalse(detector.observe(2, (3.01, 0.0, 7.0), 0.1, True, 1.2))
        self.assertTrue(detector.observe(2, (3.01, 0.0, 7.0), 0.1, True, 1.46))

    def test_sideways_collision_slide_does_not_delay_gateway_trigger(self):
        detector = coop.FollowerGatewayStallDetector(
            progress_distance=0.05,
            stall_seconds=0.08,
        )
        heading = 0.0
        self.assertFalse(detector.observe(3, (0.0, 0.0, 4.0), 0.1, True, 0.0, heading))
        # The actor slides well beyond the distance threshold along X, but
        # makes no progress along the commanded +Z direction.
        self.assertFalse(
            detector.observe(3, (0.08, 0.0, 4.0), 0.1, True, 0.04, heading)
        )
        self.assertTrue(
            detector.observe(3, (0.16, 0.0, 4.0), 0.1, True, 0.081, heading)
        )


class NativeAreaHandoffControllerTests(unittest.TestCase):
    def setUp(self):
        self.old_filter_state = coop.P2_DEVICE_FILTER_STATE
        coop.P2_DEVICE_FILTER_STATE = 0x500000
        self.process = FakeProcess()
        self.battle = 0x600000
        self.process.put_u64(session_state.BATTLE_INFO_GLOBAL, self.battle)
        self.process.put_u32(
            self.battle + session_state.BATTLE_LEADER_OFFSET,
            0,
        )
        self.process.write(
            coop.AREA_ID_TABLE,
            struct.pack("<4I", 0xF7, 0xD4, 0x15B, 0xFFFFFFFF),
        )

    def tearDown(self):
        coop.P2_DEVICE_FILTER_STATE = self.old_filter_state

    def test_promotes_follower_then_restores_original_after_area_change(self):
        detector = coop.FollowerGatewayStallDetector(
            progress_distance=0.1,
            stall_seconds=0.01,
        )
        controller = coop.NativeAreaHandoffController(detector)
        arguments = (
            self.process,
            2,
            (10.0, 0.0, 20.0),
            0.1,
            True,
            0,
            5,
        )
        self.assertFalse(controller.consider(*arguments, 0.0))
        self.assertTrue(controller.consider(*arguments, 0.02))
        self.assertEqual(controller.phase, "await_follower")
        self.assertEqual(
            self.process.u32(
                coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET
            ),
            1,
        )
        self.assertEqual(
            self.process.u32(
                coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_MEMBER_OFFSET
            ),
            5,
        )

        self.process.put_u32(
            self.battle + session_state.BATTLE_LEADER_OFFSET,
            5,
        )
        self.process.put_u32(
            coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
            controller.sequence,
        )
        controller.tick(self.process, 0.03)
        self.assertEqual(controller.phase, "hold")

        self.process.write(
            coop.AREA_ID_TABLE,
            struct.pack("<4I", 0xF8, 0xD4, 0x15B, 0xFFFFFFFF),
        )
        controller.tick(self.process, 0.04)
        self.assertEqual(controller.phase, "await_restore")
        self.assertTrue(controller.area_changed)
        self.assertEqual(
            self.process.u32(
                coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_MEMBER_OFFSET
            ),
            0,
        )

        self.process.put_u32(
            self.battle + session_state.BATTLE_LEADER_OFFSET,
            0,
        )
        self.process.put_u32(
            coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,
            controller.sequence,
        )
        controller.tick(self.process, 0.05)
        self.assertEqual(controller.phase, "idle")
        self.assertEqual(
            self.process.u32(
                coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET
            ),
            0,
        )

    def test_busy_controller_preserves_the_trigger_latch(self):
        detector = coop.FollowerGatewayStallDetector(stall_seconds=0.01)
        controller = coop.NativeAreaHandoffController(detector)
        arguments = (
            self.process,
            3,
            (4.0, 0.0, 8.0),
            0.1,
            True,
            0,
            3,
        )
        controller.consider(*arguments, 0.0)
        self.assertTrue(controller.consider(*arguments, 0.02))
        state_before = list(detector.states[3])
        self.assertFalse(
            controller.consider(
                self.process,
                3,
                (4.0, 0.0, 8.0),
                0.0,
                False,
                0,
                3,
                0.5,
            )
        )
        self.assertEqual(detector.states[3], state_before)


if __name__ == "__main__":
    unittest.main()
