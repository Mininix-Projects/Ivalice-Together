import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from headless_menu_model import ActionDefinition, EntryKind, TargetGroup
from native_menu_catalog_provider import (
    EntryBinding,
    NORMALIZED_SCHEMA,
    parse_catalog_document,
    parse_descriptor,
    project_page_definition,
)


class FakeActionCatalog:
    def __init__(self):
        self.requested = []

    def definition(self, action_id):
        self.requested.append(action_id)
        return ActionDefinition(
            action_id,
            f"Action {action_id:X}",
            "Catalog description",
            (TargetGroup.FOES,),
            TargetGroup.FOES,
            "Test",
        )


class NativeMenuCatalogProviderTests(unittest.TestCase):
    def test_raw_u16_is_dialog_index_and_never_invented_as_action_id(self):
        descriptor = parse_descriptor(
            {
                "type": 6,
                "count": 1,
                "entries": [{"raw": "9600000000000000"}],
            }
        )
        entry = descriptor.entries[0]
        self.assertEqual(entry.dialog_index, 0x96)
        self.assertIsNone(entry.action_id)

    def test_visibility_and_enabled_are_independent_tristates(self):
        descriptor = parse_descriptor(
            {
                "type": "0x21",
                "count": 3,
                "visible": True,
                "enabled": False,
                "entries": [
                    {"raw": "0100000000000000", "visible": True, "enabled": False},
                    {"raw": "0200000000000000", "visible": False, "enabled": True},
                    {"raw": "0300000000000000"},
                ],
            }
        )
        self.assertIs(descriptor.visible, True)
        self.assertIs(descriptor.enabled, False)
        self.assertEqual(
            [(entry.visible, entry.enabled) for entry in descriptor.entries],
            [(True, False), (False, True), (None, None)],
        )

    def test_unknown_type_and_count_mismatch_are_lossless(self):
        descriptor = parse_descriptor(
            {
                "type_u32": 0xDEAD,
                "descriptor_count_u32_40": 4,
                "entries": ["1000000000000000", "1100000000000000"],
            }
        )
        self.assertEqual(descriptor.type_id, 0xDEAD)
        self.assertIsNone(descriptor.mapped_type)
        self.assertFalse(descriptor.complete)
        self.assertEqual(descriptor.count_delta, -2)
        self.assertEqual(len(descriptor.entries), 2)

    def test_unknown_flag_evidence_is_preserved_but_pointers_are_not(self):
        descriptor = parse_descriptor(
            {
                "type": 6,
                "count": 1,
                "u32_08": 0x0D,
                "receiver_pointer": "0x7fff12345678",
                "entries": [
                    {
                        "raw": "1200008020304050",
                        "flags_53": 0x80,
                        "value_54": 0x20,
                        "u64_50": "0x5040302080000012",
                        "owner_address": "0x12345678",
                    }
                ],
            }
        )
        rendered = json.dumps(descriptor.as_dict())
        self.assertEqual(descriptor.unknown_flags["u32_08"], 0x0D)
        self.assertEqual(descriptor.entries[0].unknown_flags["flags_53"], 0x80)
        self.assertEqual(descriptor.entries[0].unknown_flags["value_54"], 0x20)
        self.assertNotIn("receiver_pointer", rendered)
        self.assertNotIn("owner_address", rendered)
        self.assertNotIn("u64_50", rendered)

    def test_accepts_future_builder_trace_envelope(self):
        snapshot = parse_catalog_document(
            {
                "schema": "ff12-builder-trace-v1",
                "label": "penelo",
                "generation": 7,
                "events": [
                    {
                        "discriminator": {"actor_slot": 1, "menu_generation": 7},
                        "descriptor": {
                            "menu_type": 6,
                            "count": 2,
                            "entries_raw": [
                                "0000000000000000",
                                "1200000000000000",
                            ],
                        },
                    }
                ],
            },
            type_map={6: "root"},
        )
        self.assertEqual(snapshot.label, "penelo")
        self.assertEqual(snapshot.generation, 7)
        self.assertEqual(snapshot.descriptors[0].mapped_type, "root")
        self.assertEqual(snapshot.descriptors[0].discriminator["actor_slot"], 1)
        self.assertEqual(snapshot.as_dict()["schema"], NORMALIZED_SCHEMA)

    def test_accepts_actual_builder_trace_and_applies_type_specific_semantics(self):
        snapshot = parse_catalog_document(
            {
                "schema": "ff12.native-builder-output-snapshot.v1",
                "label": "penelo_pages",
                "snapshots": [
                    {
                        "snapshot_id": 0,
                        "descriptor": "0xdeadbeef",
                        "type": 8,
                        "caller": "0x27e317",
                        "return_value": 2,
                        "count_raw": 2,
                        "count_copied": 2,
                        "capture_flags": 0,
                        "header": {
                            "decoded": {
                                "actor_or_context_u32_08": 0x00200013,
                                "discriminator_u32_14": 3,
                                "u32_18": 0,
                                "u32_1c": 0,
                            }
                        },
                        "entries": [
                            {"raw": "0000000400000000"},
                            {"raw": "0100000627000000"},
                        ],
                    },
                    {
                        "snapshot_id": 1,
                        "type": 0x0B,
                        "caller": "0x27e317",
                        "return_value": 1,
                        "count_raw": 1,
                        "count_copied": 1,
                        "capture_flags": 0,
                        "header": {
                            "decoded": {
                                "actor_or_context_u32_08": 0x00200013,
                                "discriminator_u32_14": 3,
                                "u32_18": 0,
                                "u32_1c": 0,
                            }
                        },
                        "entries": [{"raw": "5200040014080000"}],
                    },
                ],
            }
        )
        categories, items = snapshot.descriptors
        self.assertTrue(categories.entries[0].enabled)
        self.assertFalse(categories.entries[1].enabled)
        self.assertIsNone(categories.entries[0].action_id)
        self.assertEqual(items.entries[0].action_id, 0x52)
        self.assertTrue(items.entries[0].visible)
        self.assertTrue(items.entries[0].enabled)
        self.assertEqual(items.discriminator["actor_token_u32_08"], 0x00200013)
        self.assertEqual(items.discriminator["variant_u32_14"], 3)
        rendered = json.dumps(snapshot.as_dict())
        self.assertNotIn("0xdeadbeef", rendered.casefold())

    def test_accepts_legacy_read_only_snapshot_without_retaining_addresses(self):
        snapshot = parse_catalog_document(
            {
                "schema": "ff12-native-menu-snapshot-v1",
                "label": "penelo",
                "controllers": [
                    {
                        "address": "0x600000",
                        "owner": "0x500000",
                        "descriptor_candidates": [
                            {
                                "address": "0x5104c0",
                                "decoded": {
                                    "type_u32": 6,
                                    "descriptor_count_u32_40": 1,
                                    "entries": [
                                        {
                                            "raw": "1200000000000000",
                                            "u16_50": 0x12,
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        )
        rendered = json.dumps(snapshot.as_dict())
        self.assertEqual(len(snapshot.descriptors), 1)
        self.assertEqual(snapshot.descriptors[0].entries[0].dialog_index, 0x12)
        self.assertNotIn("0x600000", rendered)
        self.assertNotIn("0x5104c0", rendered)

    def test_projection_refuses_to_treat_dialog_index_as_action(self):
        descriptor = parse_descriptor(
            {
                "type": 6,
                "count": 1,
                "entries": [{"raw": "9600000000000000", "enabled": True}],
            }
        )
        catalog = FakeActionCatalog()
        projection = project_page_definition(
            descriptor,
            page_key="root",
            title="",
            bindings={0: EntryBinding(0, EntryKind.ACTION, "Attack", enabled=True)},
            action_catalog=catalog,
        )
        self.assertEqual(catalog.requested, [])
        self.assertEqual(projection.page.entries, ())
        self.assertEqual(projection.unresolved[0].reason, "action ID not observed")

    def test_projection_uses_only_explicit_action_id_and_disables_unknown_state(self):
        descriptor = parse_descriptor(
            {
                "type": 6,
                "count": 2,
                "entries": [
                    {"raw": "0000000000000000", "action_id": "0x96", "enabled": True},
                    {"raw": "1200000000000000"},
                ],
            }
        )
        catalog = FakeActionCatalog()
        projection = project_page_definition(
            descriptor,
            page_key="root",
            title="",
            bindings={
                0: EntryBinding(0, EntryKind.ACTION, "Attack"),
                1: EntryBinding(1, EntryKind.PAGE, "Magicks", page_key="magicks"),
            },
            action_catalog=catalog,
        )
        self.assertEqual(catalog.requested, [0x96])
        self.assertTrue(projection.page.entries[0].enabled)
        self.assertFalse(projection.page.entries[1].enabled)
        self.assertIn("enabled state unknown", projection.diagnostics[0])

    def test_invalid_entry_stride_fails_loudly(self):
        with self.assertRaisesRegex(ValueError, "exactly 8 bytes"):
            parse_descriptor(
                {
                    "type": 6,
                    "count": 1,
                    "entries": ["1200"],
                }
            )


if __name__ == "__main__":
    unittest.main()
