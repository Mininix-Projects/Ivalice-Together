import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from native_menu_catalog_provider import parse_catalog_document
from native_menu_trace_merge import merge_catalogs, stable_page_key


def trace(label, snapshots, captured_utc):
    return parse_catalog_document(
        {
            "schema": "ff12.native-builder-output-snapshot.v1",
            "label": label,
            "captured_utc": captured_utc,
            "snapshots": snapshots,
        }
    )


def snapshot(type_id, variant, entries, *, actor=0x00200013, result=None):
    payload = {
        "type": type_id,
        "count_raw": len(entries),
        "count_copied": len(entries),
        "return_value": len(entries) if result is None else result,
        "capture_flags": 0,
        "header": {
            "decoded": {
                "actor_or_context_u32_08": actor,
                "discriminator_u32_14": variant,
                "u32_18": 0,
                "u32_1c": 0,
            }
        },
        "entries": [{"raw": raw} for raw in entries],
    }
    return payload


class NativeMenuTraceMergeTests(unittest.TestCase):
    def test_latest_root_and_categories_ignore_volatile_variant(self):
        old = trace(
            "old",
            [
                snapshot(6, 0, ["0000000000000000"]),
                snapshot(8, 0, ["0000000400000000"]),
            ],
            "2026-07-17T10:00:00+00:00",
        )
        new = trace(
            "new",
            [
                snapshot(6, 3, ["0000000000000000", "1200000000000000"]),
                snapshot(8, 3, ["0000040400080000", "0100020627080000"]),
            ],
            "2026-07-17T10:01:00+00:00",
        )
        merged = merge_catalogs((old, new), actor_token=0x00200013, generation=9)
        self.assertEqual(merged.generation, 9)
        self.assertEqual(len(merged.descriptors), 2)
        self.assertEqual([d.declared_count for d in merged.descriptors], [2, 2])
        self.assertEqual(stable_page_key(merged.descriptors[1]), (8, 0, 0, 0))

    def test_type0b_variants_and_action_ids_survive(self):
        catalog = trace(
            "actions",
            [
                snapshot(6, 0, ["0000000000000000"]),
                snapshot(0x0B, 1, ["0000080012080000"]),
                snapshot(0x0B, 2, ["9e00080090020000"]),
                snapshot(0x0B, 3, ["5200040014080000"]),
            ],
            "2026-07-17T10:00:00+00:00",
        )
        merged = merge_catalogs((catalog,), actor_token=0x00200013)
        variants = {
            stable_page_key(d)[1]: d for d in merged.descriptors if d.type_id == 0x0B
        }
        self.assertEqual(set(variants), {1, 2, 3})
        self.assertEqual(variants[1].entries[0].action_id, 0x00)
        self.assertEqual(variants[2].entries[0].action_id, 0x9E)
        self.assertEqual(variants[3].entries[0].action_id, 0x52)

    def test_wrong_actor_and_failed_output_are_rejected(self):
        catalog = trace(
            "bad",
            [
                snapshot(6, 0, ["0000000000000000"], actor=0x00100011),
                snapshot(6, 0, ["0000000000000000"], result=-1),
            ],
            "2026-07-17T10:00:00+00:00",
        )
        with self.assertRaisesRegex(ValueError, "no complete descriptors"):
            merge_catalogs((catalog,), actor_token=0x00200013)


if __name__ == "__main__":
    unittest.main()
