import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

import menu_native_snapshot as snapshot


class FakeMemory:
    def __init__(self):
        self.segments = []

    def add(self, address, data):
        self.segments.append((address, bytes(data)))

    def read(self, address, size):
        for base, data in reversed(self.segments):
            offset = address - base
            if 0 <= offset and offset + size <= len(data):
                return data[offset : offset + size]
        raise OSError(f"unmapped read {address:#x}+{size:#x}")


def put(block, offset, fmt, value):
    struct.pack_into(fmt, block, offset, value)


def fixture(callback=snapshot.MENU_CALLBACK):
    memory = FakeMemory()
    manager = 0x400000
    menu = manager + snapshot.P1_MENU_OFFSET
    owner = 0x500000
    receiver = 0x510000
    controller = 0x600000

    memory.add(snapshot.UI_MANAGER_GLOBAL, struct.pack("<Q", manager))

    menu_block = bytearray(0x3200)
    put(menu_block, 0x00, "<Q", callback)
    put(menu_block, 0x18, "<Q", owner)
    memory.add(menu, menu_block)

    owner_block = bytearray(0x200)
    put(owner_block, 0x00, "<Q", snapshot.LIST_OWNER_CALLBACK)
    put(owner_block, 0x18, "<Q", controller)
    put(owner_block, 0xDA, "<h", 0)
    memory.add(owner, owner_block)

    controller_block = bytearray(0x180)
    put(controller_block, 0x00, "<Q", snapshot.LIST_CONTROLLER_CALLBACK)
    put(controller_block, 0x10, "<Q", owner)
    put(controller_block, 0xC8, "<Q", receiver)
    put(controller_block, 0xE8, "<H", 4)
    controller_block[0xEC] = 1
    put(controller_block, 0xED, "<b", 2)
    put(controller_block, 0xEE, "<b", 0)
    put(controller_block, 0xF2, "<h", 0)
    put(controller_block, 0xF4, "<h", 0)
    memory.add(controller, controller_block)

    receiver_block = bytearray(0x180)
    put(receiver_block, 0x00, "<Q", snapshot.LIST_PAGE_CALLBACK)
    memory.add(receiver, receiver_block)
    memory.add(receiver + 0x1510, struct.pack("<Q", controller))

    descriptor = bytearray(0x100)
    put(descriptor, 0x00, "<I", 6)
    put(descriptor, 0x08, "<I", 0x0D)
    put(descriptor, 0x1C, "<H", 0x1234)
    put(descriptor, 0x40, "<I", 4)
    for index in range(4):
        entry = struct.pack(
            "<H6B",
            0x1000 + index,
            index,
            0x80 | index,
            0x20 + index,
            0x30 + index,
            0x40 + index,
            0x50 + index,
        )
        descriptor[0x50 + index * 8 : 0x58 + index * 8] = entry
    memory.add(receiver + snapshot.DESCRIPTOR_IN_PAGE, descriptor)
    memory.add(owner + snapshot.DESCRIPTOR_FROM_OWNER, descriptor)
    memory.add(
        owner + snapshot.DESCRIPTOR_FROM_OWNER + snapshot.DESCRIPTOR_PAGE_STRIDE,
        bytes(0x100),
    )

    for address in (
        snapshot.MENU_CALLBACK,
        snapshot.LIST_OWNER_CALLBACK,
        snapshot.LIST_CONTROLLER_CALLBACK,
        snapshot.DESCRIPTION_RESOLVER,
    ):
        memory.add(address, bytes.fromhex("48 89 5c 24 18"))
    return memory, menu, receiver, controller


class NativeMenuSnapshotTests(unittest.TestCase):
    def test_access_mask_has_no_mutation_rights(self):
        forbidden = snapshot.PROCESS_VM_WRITE | snapshot.PROCESS_VM_OPERATION
        self.assertEqual(snapshot.READ_ONLY_ACCESS_MASK & forbidden, 0)
        self.assertFalse(hasattr(snapshot.ReadOnlyProcess, "write"))
        self.assertFalse(hasattr(snapshot.ReadOnlyProcess, "patch_code"))

    def test_capture_correlates_receiver_descriptor_and_index(self):
        memory, menu, receiver, controller = fixture()
        result = snapshot.capture_snapshot(memory, 1234, "penelo")
        self.assertEqual(result["menu"]["address"], hex(menu))
        self.assertEqual(result["menu"]["raw"]["size"], 0x3200)
        self.assertEqual(len(result["controllers"]), 1)

        item = result["controllers"][0]
        self.assertEqual(item["address"], hex(controller))
        self.assertEqual(item["count"], 4)
        self.assertEqual(item["selected"], 2)
        self.assertTrue(item["receiver_object"]["callback_matches"])
        self.assertTrue(item["receiver_object"]["controller_slot_matches"])

        primary = item["descriptor_candidates"][0]
        self.assertEqual(
            primary["address"],
            hex(receiver + snapshot.DESCRIPTOR_IN_PAGE),
        )
        self.assertEqual(primary["header_raw"]["size"], 0x50)
        decoded = primary["decoded"]
        self.assertTrue(decoded["count_matches_controller"])
        self.assertEqual(decoded["descriptor_count_u32_40"], 4)
        self.assertEqual(decoded["selected_entry"]["u16_50"], 0x1002)
        self.assertEqual(len(decoded["selected_entry"]["bytes_53_57"]), 5)

    def test_non_live_menu_fails_before_tree_walk(self):
        memory, menu, receiver, controller = fixture(callback=0)
        with self.assertRaisesRegex(RuntimeError, "is not live"):
            snapshot.capture_snapshot(memory, 1234, "closed")


if __name__ == "__main__":
    unittest.main()
