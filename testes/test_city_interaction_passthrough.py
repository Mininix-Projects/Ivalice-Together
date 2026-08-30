"""Co-op must leave stock P1 world interactions untouched while dormant."""

import struct
import unittest

import coop_runtime as coop


class MemoryProcess:
    def __init__(self):
        self.data = {}
        self.regions = {}

    def read(self, address, size):
        return bytes(self.data.get(address + offset, 0) for offset in range(size))

    def write(self, address, payload):
        payload = bytes(payload)
        self.regions[address] = payload
        for offset, value in enumerate(payload):
            self.data[address + offset] = value

    def patch_code(self, address, payload):
        self.write(address, payload)

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]


def disabled_routing_target(code: bytes) -> int:
    guard = (
        b"\x48\xb8"
        + struct.pack("<Q", coop.COMMAND_BLOCK)
        + b"\x83\x78"
        + bytes((coop.INTERACTION_ROUTING_ACTIVE_OFFSET,))
        + b"\x00\x75\x05\xe9"
    )
    guard_at = code.index(guard)
    jump_at = guard_at + len(guard) - 1
    displacement = struct.unpack_from("<i", code, jump_at + 1)[0]
    return jump_at + 5 + displacement


def stock_cleanup_offset(code: bytes) -> int:
    cleanup = (
        b"\x49\xbb"
        + struct.pack("<Q", coop.DUAL_INTERACTION_STATE)
        + b"\x41\xc7\x03\x00\x00\x00\x00"
        + b"\x49\xba"
        + struct.pack("<Q", coop.interaction_scan_entry_address())
        + b"\x49\xc7\x02\x00\x00\x00\x00"
    )
    return code.index(cleanup)


class CityInteractionPolicyTests(unittest.TestCase):
    def test_experimental_follower_npc_route_is_off_in_production(self):
        self.assertFalse(coop.FOLLOWER_NPC_INTERACTIONS_ENABLED)

    def test_town_or_modal_session_disables_private_follower_scans(self):
        for p2_owned, p3_owned in ((True, False), (False, True), (True, True)):
            self.assertFalse(
                coop.follower_interaction_routing_active(
                    True,
                    True,
                    p2_owned,
                    p3_owned,
                    town=True,
                )
            )

    def test_non_town_session_gate_still_blocks_modal_or_loading_state(self):
        self.assertFalse(
            coop.follower_interaction_routing_active(
                True,
                False,
                True,
                True,
                town=False,
            )
        )

    def test_field_session_with_an_owned_follower_enables_private_scans(self):
        self.assertTrue(
            coop.follower_interaction_routing_active(
                True, True, True, False, town=False
            )
        )
        self.assertTrue(
            coop.follower_interaction_routing_active(
                True, True, False, True, town=False
            )
        )

    def test_enabled_coop_without_an_owned_follower_keeps_stock_route(self):
        self.assertFalse(
            coop.follower_interaction_routing_active(
                True, True, False, False, town=False
            )
        )

    def test_interaction_flag_is_independent_from_logical_coop_flag(self):
        process = MemoryProcess()
        coop.publish_lifecycle_flags(process, True, True, False)

        self.assertEqual(process.u32(coop.COMMAND_BLOCK + coop.COOP_ACTIVE_OFFSET), 1)
        self.assertEqual(process.u32(coop.COMMAND_BLOCK + coop.CAMERA_ACTIVE_OFFSET), 1)
        self.assertEqual(
            process.u32(coop.COMMAND_BLOCK + coop.INTERACTION_ROUTING_ACTIVE_OFFSET),
            0,
        )

    def test_interaction_flag_uses_free_command_block_space(self):
        self.assertGreaterEqual(coop.INTERACTION_ROUTING_ACTIVE_OFFSET, 0x58)
        self.assertLessEqual(
            coop.INTERACTION_ROUTING_ACTIVE_OFFSET + 4,
            coop.CAMERA_COLLISION_EXCLUSION_START_OFFSET,
        )


class StockInteractionPassthroughAssemblyTests(unittest.TestCase):
    def setUp(self):
        coop.set_dual_interaction_base(0x60000000)

    def test_generic_door_and_crystal_scan_skips_both_private_owners(self):
        process = MemoryProcess()
        process.write(
            coop.INTERACTION_DUAL_SCAN_CALL_SITE,
            coop.INTERACTION_DUAL_SCAN_CALL_ORIGINAL,
        )
        coop.install_dual_interaction_scan(process)
        cave = process.regions[coop.DUAL_INTERACTION_SCAN_CAVE]

        self.assertEqual(disabled_routing_target(cave), stock_cleanup_offset(cave))

    def test_exact_npc_scan_skips_both_private_owners(self):
        cave = coop._build_follower_npc_exact_scan_cave()

        self.assertEqual(disabled_routing_target(cave), stock_cleanup_offset(cave))

    def test_both_passthroughs_clear_private_owner_before_stock_scan(self):
        process = MemoryProcess()
        process.write(
            coop.INTERACTION_DUAL_SCAN_CALL_SITE,
            coop.INTERACTION_DUAL_SCAN_CALL_ORIGINAL,
        )
        coop.install_dual_interaction_scan(process)
        caves = (
            process.regions[coop.DUAL_INTERACTION_SCAN_CAVE],
            coop._build_follower_npc_exact_scan_cave(),
        )

        for cave in caves:
            target = disabled_routing_target(cave)
            self.assertEqual(
                cave[target + 10 : target + 17], b"\x41\xc7\x03" + b"\x00" * 4
            )


if __name__ == "__main__":
    unittest.main()
