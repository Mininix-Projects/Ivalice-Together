import inspect
import struct
import unittest

import coop_runtime as coop
from capstone import CS_ARCH_X86, CS_MODE_64, Cs


class FakeProcess:
    def __init__(self):
        self.writes = []
        self.patches = []

    def write(self, address, data):
        self.writes.append((address, bytes(data)))

    def read(self, address, size):
        if address == coop.XINPUT_DEVICE_FINALIZE_SITE:
            return coop.XINPUT_DEVICE_FINALIZE_ORIGINAL[:size]
        if address == coop.XINPUT_STATE_RESULT_SITE:
            return coop.XINPUT_STATE_RESULT_ORIGINAL[:size]
        if address == coop.DINPUT_GAMEPAD_FINALIZE_SITE:
            return coop.DINPUT_GAMEPAD_FINALIZE_ORIGINAL[:size]
        if address == coop.STEAM_GAMEPAD_FINALIZE_SITE:
            return coop.STEAM_GAMEPAD_FINALIZE_ORIGINAL[:size]
        if address == coop.INTERACTION_DUAL_SCAN_CALL_SITE:
            return coop.INTERACTION_DUAL_SCAN_CALL_ORIGINAL[:size]
        if address == coop.KEYBOARD_HOOK_SITE:
            return b"\x48\x8b\xce\xe8\x3e\xfe\xff\xff"[:size]
        if address == coop.INTERACTION_PROXIMITY_SOURCE_SITE:
            return coop.INTERACTION_PROXIMITY_SOURCE_ORIGINAL[:size]
        if address == coop.NATIVE_PARTY_LEADER_SETTER:
            return coop.NATIVE_PARTY_LEADER_SETTER_ORIGINAL[:size]
        return b"\x00" * size

    def patch_code(self, address, data):
        self.patches.append((address, bytes(data)))

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]


class ThreePlayerDeviceIsolationTests(unittest.TestCase):
    def test_mask_accepts_both_coop_xinput_slots(self):
        self.assertEqual(coop.controller_isolation_mask((1, 2)), 0b0110)
        self.assertEqual(coop.controller_isolation_mask((2, 2)), 0b0100)
        self.assertEqual(coop.controller_isolation_mask(()), 0)

    def test_native_game_keeps_only_the_primary_controller(self):
        self.assertEqual(coop.native_controller_isolation_indices(0), (1, 2, 3))
        self.assertEqual(coop.native_controller_isolation_indices(2), (0, 1, 3))
        self.assertEqual(coop.native_controller_isolation_indices(None), (0, 1, 2, 3))

    def test_native_slot_follows_any_assigned_xinput_device(self):
        for slot in range(4):
            with self.subTest(slot=slot):
                self.assertEqual(coop.native_controller_slot(slot), slot)
        self.assertEqual(
            coop.native_controller_slot({"backend": "sdl", "guid": "dualsense"}),
            coop.P1_SDL_XINPUT_VIRTUAL_SLOT,
        )
        self.assertEqual(
            coop.native_controller_slot(None),
            coop.INVALID_NATIVE_CONTROLLER_SLOT,
        )

    def test_native_slot_publisher_uses_its_own_atomic_word(self):
        process = FakeProcess()
        self.assertEqual(coop.publish_native_controller_slot(process, 3), 3)
        self.assertEqual(
            process.writes,
            [
                (
                    coop.P2_DEVICE_FILTER_STATE + coop.P1_NATIVE_CONTROLLER_SLOT_OFFSET,
                    struct.pack("<I", 3),
                )
            ],
        )

    def test_sdl_p1_reserves_the_internal_virtual_slot(self):
        assignment = {"backend": "sdl", "guid": "dualsense", "ordinal": 0}
        self.assertEqual(
            coop.native_controller_isolation_indices(assignment), (1, 2, 3)
        )

    def test_p1_sdl_bridge_substitutes_the_complete_xinput_result(self):
        coop.set_dual_interaction_base(0x51000000)
        bridge = coop.build_p1_sdl_xinput_cave()
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        instructions = list(decoder.disasm(bridge, coop.P1_SDL_XINPUT_CAVE))

        self.assertEqual(sum(item.size for item in instructions), len(bridge))
        self.assertLessEqual(len(bridge), coop.P1_SDL_XINPUT_CAVE_SIZE)
        self.assertTrue(
            any(
                item.mnemonic == "movups" and "[rsp + 0x38]" in item.op_str
                for item in instructions
            )
        )
        self.assertTrue(
            any(
                item.mnemonic == "xor" and item.op_str == "eax, eax"
                for item in instructions
            )
        )
        self.assertEqual(instructions[-2].bytes, coop.XINPUT_STATE_RESULT_ORIGINAL)
        self.assertEqual(instructions[-1].mnemonic, "jmp")

    def test_p1_sdl_publisher_commits_a_finished_inactive_buffer_first(self):
        coop.set_dual_interaction_base(0x51000000)
        process = FakeProcess()
        active = coop.publish_p1_sdl_xinput_state(
            process,
            bytes(range(12)),
            packet_number=7,
            active_buffer=0,
        )

        self.assertEqual(active, 1)
        self.assertEqual(
            process.writes,
            [
                (
                    coop.P1_SDL_XINPUT_STATE + coop.P1_SDL_XINPUT_BUFFER_1_OFFSET,
                    struct.pack("<I", 7) + bytes(range(12)),
                ),
                (
                    coop.P1_SDL_XINPUT_STATE + coop.P1_SDL_XINPUT_SLOT_OFFSET,
                    struct.pack("<I", coop.P1_SDL_XINPUT_VIRTUAL_SLOT),
                ),
                (
                    coop.P1_SDL_XINPUT_STATE + coop.P1_SDL_XINPUT_ACTIVE_BUFFER_OFFSET,
                    struct.pack("<I", 1),
                ),
                (
                    coop.P1_SDL_XINPUT_STATE + coop.P1_SDL_XINPUT_ENABLED_OFFSET,
                    struct.pack("<I", 1),
                ),
            ],
        )

    def test_publisher_commits_one_atomic_mask_word(self):
        process = FakeProcess()
        coop.publish_coop_device_isolation(process, (1, 2))
        self.assertEqual(
            process.writes,
            [
                (
                    coop.P2_DEVICE_FILTER_STATE,
                    struct.pack("<I", 0b0110),
                )
            ],
        )

    def test_runtime_isolates_every_slot_except_p1(self):
        source = inspect.getsource(coop.main)
        self.assertNotIn('bindings.assigned_xinput("p2")', source)
        self.assertNotIn('bindings.assigned_xinput("p3")', source)
        self.assertIn(
            "native_controller_isolation_indices(assigned_p1_device)",
            source,
        )
        self.assertIn("publish_coop_device_isolation", source)

    def test_device_filter_cave_tests_each_native_device_against_the_mask(self):
        process = FakeProcess()
        coop.install_p2_device_isolation(process)
        cave = next(
            data
            for address, data in process.writes
            if (
                address == coop.P2_DEVICE_FILTER_CAVE
                and len(data) > coop.P2_DEVICE_FILTER_STATE_SIZE
            )
        )
        self.assertIn(b"\x0f\xa3\xc8", cave)  # bt eax,ecx
        self.assertEqual(cave.count(b"\x8b\x83\x58\x04\x00\x00"), 2)
        self.assertNotIn(b"\x83\xbb\x58\x04\x00\x00\x00", cave)
        self.assertLessEqual(len(cave), 0x200)
        self.assertTrue(process.patches)

    def test_dinput_filter_clears_platform_data_then_runs_native_filters(self):
        coop.set_dual_interaction_base(0x51000000)
        process = FakeProcess()
        coop.install_dinput_gamepad_filter(process)
        cave = next(
            data
            for address, data in process.writes
            if address == coop.DINPUT_GAMEPAD_FILTER_CAVE
        )
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        instructions = list(decoder.disasm(cave, coop.DINPUT_GAMEPAD_FILTER_CAVE))

        self.assertEqual(sum(item.size for item in instructions), len(cave))
        self.assertLessEqual(len(cave), coop.DINPUT_GAMEPAD_FILTER_CAVE_SIZE)
        self.assertIn(
            b"\x49\xbb" + struct.pack("<Q", coop.P2_DEVICE_FILTER_STATE),
            cave,
        )
        for offset in (0x414, 0x418, 0x420, 0x424, 0x43C, 0x444):
            self.assertTrue(
                any(
                    item.mnemonic == "mov"
                    and f"[rbx + {offset:#x}]" in item.op_str
                    for item in instructions
                ),
                f"missing m_platformData clear at {offset:#x}",
            )
        self.assertIn(
            ("call", f"{coop.XINPUT_DEVICE_FINALIZER:#x}"),
            tuple((item.mnemonic, item.op_str) for item in instructions),
        )
        self.assertIn(
            (
                coop.DINPUT_GAMEPAD_FINALIZE_SITE,
                b"\xe9"
                + coop.rel32(
                    coop.DINPUT_GAMEPAD_FINALIZE_SITE,
                    5,
                    coop.DINPUT_GAMEPAD_FILTER_CAVE,
                )
                + b"\x90" * 3,
            ),
            process.patches,
        )

    def test_dinput_filter_is_distinct_from_keyboard_and_mouse_updates(self):
        self.assertEqual(coop.DINPUT_GAMEPAD_FINALIZE_SITE, 0x7A1C9F)
        self.assertNotEqual(coop.DINPUT_GAMEPAD_FINALIZE_SITE, coop.KEYBOARD_HOOK_SITE)
        self.assertEqual(len(coop.DINPUT_GAMEPAD_FINALIZE_ORIGINAL), 8)

    def test_steam_input_filter_clears_platform_data_then_runs_native_filters(self):
        coop.set_dual_interaction_base(0x51000000)
        process = FakeProcess()
        coop.install_steam_gamepad_filter(process)
        cave = next(
            data
            for address, data in process.writes
            if address == coop.STEAM_GAMEPAD_FILTER_CAVE
        )
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        instructions = list(decoder.disasm(cave, coop.STEAM_GAMEPAD_FILTER_CAVE))

        self.assertEqual(sum(item.size for item in instructions), len(cave))
        self.assertLessEqual(len(cave), coop.STEAM_GAMEPAD_FILTER_CAVE_SIZE)
        for offset in (0x414, 0x418, 0x420, 0x424, 0x43C, 0x444):
            self.assertTrue(
                any(
                    item.mnemonic == "mov"
                    and f"[rbx + {offset:#x}]" in item.op_str
                    for item in instructions
                ),
                f"missing m_platformData clear at {offset:#x}",
            )
        self.assertIn(
            ("call", f"{coop.XINPUT_DEVICE_FINALIZER:#x}"),
            tuple((item.mnemonic, item.op_str) for item in instructions),
        )
        self.assertEqual(instructions[-1].mnemonic, "jmp")
        self.assertIn(
            (
                coop.STEAM_GAMEPAD_FINALIZE_SITE,
                b"\xe9"
                + coop.rel32(
                    coop.STEAM_GAMEPAD_FINALIZE_SITE,
                    5,
                    coop.STEAM_GAMEPAD_FILTER_CAVE,
                )
                + b"\x90" * 3,
            ),
            process.patches,
        )

    def test_xinput_filter_never_calls_the_native_leader_setter(self):
        process = FakeProcess()
        coop.install_p2_device_isolation(process)
        cave = next(
            data
            for address, data in process.writes
            if (
                address == coop.P2_DEVICE_FILTER_CAVE
                and len(data) > coop.P2_DEVICE_FILTER_STATE_SIZE
            )
        )
        self.assertNotIn(
            b"\x48\xb8" + struct.pack("<Q", coop.NATIVE_LEADER_SETTER),
            cave,
        )

    def test_field_tick_bridge_calls_stock_setter_and_acknowledges(self):
        coop.set_cave_base(0x50000000)
        coop.set_dual_interaction_base(0x51000000)
        bridge = coop.build_native_leader_bridge_cave()
        self.assertLessEqual(len(bridge), coop.NATIVE_LEADER_BRIDGE_CAVE_SIZE)
        self.assertIn(
            b"\x48\xb8" + struct.pack("<Q", coop.NATIVE_LEADER_SETTER),
            bridge,
        )
        self.assertIn(
            b"\x41\x8b\x4b" + bytes((coop.NATIVE_LEADER_MEMBER_OFFSET,)),
            bridge,
        )
        self.assertIn(
            b"\x41\x89\x4b" + bytes((coop.NATIVE_LEADER_APPLIED_SEQUENCE_OFFSET,)),
            bridge,
        )
        self.assertIn(b"\x48\x83\xec\x28", bridge)
        self.assertIn(b"\x48\x83\xc4\x28", bridge)

    def test_field_tick_bridge_calls_complete_native_area_wrapper(self):
        coop.set_cave_base(0x50000000)
        coop.set_dual_interaction_base(0x51000000)
        bridge = coop.build_native_leader_bridge_cave()
        self.assertLessEqual(len(bridge), coop.NATIVE_LEADER_BRIDGE_CAVE_SIZE)
        self.assertIn(
            b"\x48\xb8" + struct.pack("<Q", coop.NATIVE_AREA_REQUEST_WRAPPER),
            bridge,
        )
        for opcode, offset in (
            (b"\x41\x8b\x4b", coop.NATIVE_AREA_TARGET_OFFSET),
            (b"\x41\x8b\x53", coop.NATIVE_AREA_SPAWN_OFFSET),
            (b"\x45\x8b\x43", coop.NATIVE_AREA_OPTIONS_OFFSET),
            (b"\x45\x8b\x4b", coop.NATIVE_AREA_FLAG_OFFSET),
        ):
            self.assertIn(opcode + bytes((offset,)), bridge)
        self.assertIn(
            b"\x41\x89\x43" + bytes((coop.NATIVE_AREA_RESULT_OFFSET,)),
            bridge,
        )
        self.assertIn(
            b"\x41\x89\x43" + bytes((coop.NATIVE_AREA_APPLIED_SEQUENCE_OFFSET,)),
            bridge,
        )

    def test_field_tick_bridge_fully_disassembles(self):
        coop.set_cave_base(0x50000000)
        coop.set_dual_interaction_base(0x51000000)
        bridge = coop.build_native_leader_bridge_cave()
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        instructions = list(decoder.disasm(bridge, coop.NATIVE_LEADER_BRIDGE_CAVE))
        self.assertEqual(sum(item.size for item in instructions), len(bridge))
        self.assertEqual(instructions[-1].mnemonic, "ret")

    def test_native_party_leader_guard_blocks_every_stock_request_during_coop(self):
        coop.set_cave_base(0x50000000)
        coop.set_dual_interaction_base(0x51000000)
        cave = coop.build_native_party_leader_guard_cave()
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        instructions = list(decoder.disasm(cave, coop.NATIVE_PARTY_LEADER_GUARD_CAVE))

        self.assertEqual(sum(item.size for item in instructions), len(cave))
        self.assertLessEqual(len(cave), 0x40)
        operands = tuple(item.op_str for item in instructions)
        self.assertIn("dword ptr [rax + 8], 0", operands)
        self.assertNotIn(
            b"\x48\xb8" + struct.pack("<Q", coop.P2_DEVICE_FILTER_STATE),
            cave,
        )
        self.assertEqual(
            b"".join(item.bytes for item in instructions if item.mnemonic == "push"),
            b"\x40\x53",
        )
        self.assertEqual(sum(item.mnemonic == "ret" for item in instructions), 1)
        self.assertFalse(
            any(
                item.mnemonic.startswith("j")
                and item.mnemonic != "jmp"
                and int(item.op_str, 16) < item.address
                for item in instructions
            )
        )

    def test_native_party_leader_guard_is_installed_at_the_ui_setter(self):
        coop.set_cave_base(0x50000000)
        coop.set_dual_interaction_base(0x51000000)
        process = FakeProcess()

        coop.install_native_party_leader_guard(process)

        self.assertIn(
            (
                coop.NATIVE_PARTY_LEADER_GUARD_CAVE,
                coop.build_native_party_leader_guard_cave(),
            ),
            process.writes,
        )
        self.assertEqual(
            process.patches,
            [
                (
                    coop.NATIVE_PARTY_LEADER_SETTER,
                    b"\xe9"
                    + coop.rel32(
                        coop.NATIVE_PARTY_LEADER_SETTER,
                        5,
                        coop.NATIVE_PARTY_LEADER_GUARD_CAVE,
                    )
                    + b"\x90",
                )
            ],
        )

    def test_interaction_scan_calls_field_tick_bridge_after_stack_alignment(self):
        coop.set_cave_base(0x50000000)
        coop.set_dual_interaction_base(0x51000000)
        process = FakeProcess()
        coop.install_dual_interaction_scan(process)
        scan = next(
            data
            for address, data in process.writes
            if address == coop.DUAL_INTERACTION_SCAN_CAVE
        )
        expected_call = b"\xe8" + coop.rel32(
            coop.DUAL_INTERACTION_SCAN_CAVE + 4,
            5,
            coop.NATIVE_LEADER_BRIDGE_CAVE,
        )
        self.assertEqual(scan[:4], b"\x48\x83\xec\x38")
        self.assertEqual(scan[4:9], expected_call)
        self.assertTrue(
            any(
                address == coop.NATIVE_LEADER_BRIDGE_CAVE
                and data == coop.build_native_leader_bridge_cave()
                for address, data in process.writes
            )
        )

    def test_native_leader_request_publishes_payload_before_sequence(self):
        process = FakeProcess()
        original_read = process.read
        process.read = lambda address, size: (
            struct.pack("<I", coop.NATIVE_LEADER_HANDOFF_TRANSIENT)
            if address
            == coop.P2_DEVICE_FILTER_STATE
            + coop.NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET
            else original_read(address, size)
        )
        self.assertTrue(coop.claim_native_leader_transient_handoff(process))
        process.writes.clear()
        sequence = coop.request_native_leader_refresh(process, 5)
        self.assertEqual(sequence, 1)
        self.assertEqual(
            process.writes[-2:],
            [
                (
                    coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_MEMBER_OFFSET,
                    struct.pack("<I", 5),
                ),
                (
                    coop.P2_DEVICE_FILTER_STATE
                    + coop.NATIVE_LEADER_REQUEST_SEQUENCE_OFFSET,
                    struct.pack("<I", 1),
                ),
            ],
        )

    def test_native_area_request_publishes_arguments_before_sequence(self):
        process = FakeProcess()
        sequence = coop.request_native_area_transition(
            process,
            350,
            1,
            options=2,
            flag=1,
        )
        self.assertEqual(sequence, 1)
        self.assertEqual(
            process.writes[-2:],
            [
                (
                    coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_AREA_TARGET_OFFSET,
                    struct.pack("<4I", 350, 1, 2, 1),
                ),
                (
                    coop.P2_DEVICE_FILTER_STATE
                    + coop.NATIVE_AREA_REQUEST_SEQUENCE_OFFSET,
                    struct.pack("<I", 1),
                ),
            ],
        )

    def test_native_area_request_rejects_invalid_destination(self):
        process = FakeProcess()
        self.assertEqual(
            coop.request_native_area_transition(process, 0xFFFFFFFF, 1),
            0,
        )
        self.assertEqual(coop.request_native_area_transition(process, 350, -1), 0)
        self.assertEqual(process.writes, [])

    def test_transient_handoff_flag_is_an_independent_atomic_word(self):
        process = FakeProcess()
        coop.publish_native_leader_handoff_active(process, True)
        self.assertEqual(
            process.writes[-1],
            (
                coop.P2_DEVICE_FILTER_STATE + coop.NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET,
                struct.pack("<I", 1),
            ),
        )
        coop.publish_native_leader_handoff_active(process, False)
        self.assertEqual(process.writes[-1][1], struct.pack("<I", 0))

    def test_coop_flee_is_mirrored_only_to_the_primary_native_trigger(self):
        process = FakeProcess()
        coop.install_p2_device_isolation(process)
        cave = next(
            data
            for address, data in process.writes
            if (
                address == coop.P2_DEVICE_FILTER_CAVE
                and len(data) > coop.P2_DEVICE_FILTER_STATE_SIZE
            )
        )
        self.assertIn(
            b"\x49\xbb" + struct.pack("<Q", coop.P2_DEVICE_FILTER_STATE),
            cave,
        )
        self.assertIn(
            b"\x41\x8b\x43" + bytes((coop.COOP_FLEE_REQUEST_OFFSET,)),
            cave,
        )
        self.assertIn(
            b"\x41\x0b\x43" + bytes((coop.COOP_FLEE_P1_PHYSICAL_OFFSET,)),
            cave,
        )
        self.assertIn(
            b"\x41\x83\x7b" + bytes((coop.COOP_FLEE_ACTIVE_OFFSET,)) + b"\x00",
            cave,
        )
        self.assertIn(b"\x0f\x95\xc0", cave)
        self.assertIn(b"\x88\x83\x43\x04\x00\x00", cave)
        self.assertNotIn(b"\xc6\x83\x43\x04\x00\x00\x01", cave)

    def test_native_keyboard_filter_uses_helper_lifetime_not_field_state(self):
        process = FakeProcess()
        coop.install_keyboard_filter(process)
        cave = next(
            data for address, data in process.writes if address == coop.KEYBOARD_CAVE
        )
        self.assertIn(
            b"\x41\x83\x7b" + bytes((coop.KEYBOARD_FILTER_ACTIVE_OFFSET,)) + b"\x00",
            cave,
        )
        self.assertNotIn(
            b"\x49\x89\x73\x70",
            cave,
        )
        self.assertNotIn(
            b"\x41\x83\x7b" + bytes((coop.COOP_ACTIVE_OFFSET,)) + b"\x00",
            cave,
        )

    def test_proximity_source_resolves_current_generation_handle_at_use(self):
        process = FakeProcess()
        coop.set_cave_base(0x50000000)
        coop.set_dual_interaction_base(0x51000000)
        coop.install_p2_interaction_proximity_source(process)
        cave = next(
            data
            for address, data in process.writes
            if address == coop.INTERACTION_PROXIMITY_SOURCE_CAVE
        )
        self.assertIn(
            b"\x49\xbb" + struct.pack("<Q", coop.DUAL_INTERACTION_STATE),
            cave,
        )
        self.assertIn(
            b"\x49\xbb"
            + struct.pack("<Q", coop.COMMAND_BLOCK)
            + b"\x41\x8b\x4b"
            + bytes((coop.P2_LOOT_HANDLE_OFFSET,)),
            cave,
        )
        self.assertIn(
            b"\x49\xbb"
            + struct.pack("<Q", coop.third_player_block())
            + b"\x41\x8b\x4b"
            + bytes((coop.P3_LOOT_HANDLE_OFFSET,)),
            cave,
        )
        self.assertNotIn(struct.pack("<Q", coop.interaction_scan_entry_address()), cave)
        calls = []
        for offset in range(len(cave) - 4):
            if cave[offset] == 0xE8:
                displacement = struct.unpack_from("<i", cave, offset + 1)[0]
                calls.append(
                    coop.INTERACTION_PROXIMITY_SOURCE_CAVE
                    + offset
                    + 5
                    + displacement
                )
        self.assertEqual(calls.count(coop.ENTITY_ENTRY_RESOLVER), 1)
        self.assertEqual(calls.count(coop.ENTITY_HANDLE_RESOLVER), 0)
        # Resolver failure branches to the fixed native global, never to a
        # mailbox pointer.  Every volatile XMM register is bracketed as well.
        self.assertIn(b"\x48\x85\xc0\x0f\x84", cave)
        self.assertEqual(cave.count(b"\xf3\x0f\x7f"), 6)
        self.assertEqual(cave.count(b"\xf3\x0f\x6f"), 6)
        self.assertTrue(
            any(
                address == coop.INTERACTION_PROXIMITY_SOURCE_SITE
                for address, _data in process.patches
            )
        )

    def test_geometry_source_has_the_same_live_handle_contract(self):
        coop.set_cave_base(0x50000000)
        coop.set_dual_interaction_base(0x51000000)
        cave = coop._build_live_interaction_source_cave(
            coop.INTERACTION_SOURCE_CAVE,
            coop.INTERACTION_SOURCE_RETURN,
            output_register="rax",
            trailing_original=coop.INTERACTION_SOURCE_ORIGINAL[7:],
        )
        self.assertNotIn(struct.pack("<Q", coop.interaction_scan_entry_address()), cave)
        self.assertIn(
            b"\x49\xbb"
            + struct.pack("<Q", coop.COMMAND_BLOCK)
            + b"\x41\x8b\x4b"
            + bytes((coop.P2_LOOT_HANDLE_OFFSET,)),
            cave,
        )
        self.assertIn(b"\x48\x85\xc0\x0f\x84", cave)
        self.assertEqual(cave[-9:-5], coop.INTERACTION_SOURCE_ORIGINAL[7:])
        self.assertEqual(cave[-5], 0xE9)


if __name__ == "__main__":
    unittest.main()
