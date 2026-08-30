import unittest

from native_type0b_entry import (
    ITEMS_DISCRIMINATOR,
    decode_type0b_entry,
)


class Type0BEntryTests(unittest.TestCase):
    def test_items_row_exposes_final_action_and_quantity(self):
        # Captured Penelo Potion row: action 0x52, quantity 4.
        entry = decode_type0b_entry(
            "5200040014080000", discriminator=ITEMS_DISCRIMINATOR
        )
        self.assertEqual(entry.action_id, 0x52)
        self.assertEqual(entry.inventory_quantity, 4)
        self.assertEqual(entry.meta_u16, 0x0814)
        self.assertTrue(entry.enabled)

    def test_magic_resource_value_is_not_called_inventory_quantity(self):
        # Captured Cure row.  Meta bit 1 proves the builder populated +2 via
        # its resource-value path; the decoder deliberately does not assume
        # that every type-0x0B aux word is MP.
        entry = decode_type0b_entry("0000080012080000", discriminator=0)
        self.assertEqual(entry.action_id, 0)
        self.assertEqual(entry.resource_value, 8)
        self.assertIsNone(entry.inventory_quantity)

    def test_technick_aux_can_be_residual(self):
        # First Aid followed Cure in the same descriptor storage and retained
        # aux=8.  With no resource-populated meta bit, it is not authoritative.
        entry = decode_type0b_entry("9e00080090020000", discriminator=2)
        self.assertEqual(entry.action_id, 0x9E)
        self.assertEqual(entry.aux_kind, "opaque_or_residual")
        self.assertIsNone(entry.resource_value)

    def test_disabled_is_meta_bit_zero(self):
        entry = decode_type0b_entry("5200010001080000", discriminator=3)
        self.assertTrue(entry.disabled)
        self.assertFalse(entry.enabled)

    def test_rejects_non_enumerated_action(self):
        with self.assertRaises(ValueError):
            decode_type0b_entry("1601000000000000")  # 0x116, loop upper bound


if __name__ == "__main__":
    unittest.main()
