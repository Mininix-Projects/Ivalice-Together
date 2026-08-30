from __future__ import annotations

import struct
import unittest

from live_inventory import ITEM_SLOT_COUNT, decode_item_counters


class LiveInventoryTests(unittest.TestCase):
    def test_decodes_quantity_and_action_mapping(self):
        raw = bytearray(ITEM_SLOT_COUNT * 2)
        struct.pack_into("<H", raw, 0 * 2, 2)
        struct.pack_into("<H", raw, 9 * 2, 6)
        struct.pack_into("<H", raw, 43 * 2, 2)
        items = decode_item_counters(bytes(raw))
        self.assertEqual(
            [(item.action_id, item.quantity) for item in items],
            [(0x52, 2), (0x5B, 6), (0x7D, 2)],
        )

    def test_ignores_zero_slots_and_separates_upper_bits(self):
        raw = bytearray(ITEM_SLOT_COUNT * 2)
        struct.pack_into("<H", raw, 1 * 2, (3 << 10) | 17)
        items = decode_item_counters(bytes(raw))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].quantity, 17)
        self.assertEqual(items[0].equipped, 3)


if __name__ == "__main__":
    unittest.main()
