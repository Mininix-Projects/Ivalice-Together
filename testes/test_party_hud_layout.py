from __future__ import annotations

import os
import struct
import sys
import unittest
from pathlib import Path


COOP = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(COOP))

import party_hud_layout as hud  # noqa: E402


PACK = Path(os.environ.get("FF12_COOP_MRP_PACK", "__missing_mrppack_ys.bin"))


class FakeProcess:
    def __init__(self, section: bytes, base: int = 0x30000000) -> None:
        self.base = base
        self.memory: dict[int, int] = {
            base + index: value for index, value in enumerate(section)
        }
        pointer = hud.MRP_SECTION_TABLE + hud.FIELD_SECTION_ID * 8
        self.memory.update(
            {
                pointer + index: value
                for index, value in enumerate(struct.pack("<Q", base))
            }
        )
        self.reads = 0
        self.writes = 0

    def read(self, address: int, size: int) -> bytes:
        self.reads += 1
        return bytes(self.memory[address + index] for index in range(size))

    def write(self, address: int, value: bytes) -> None:
        for index, byte in enumerate(bytes(value)):
            self.memory[address + index] = byte
        self.writes += 1

    def u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]


def live_field_section() -> bytes:
    pack = PACK.read_bytes()
    section_offset = struct.unpack_from("<I", pack, hud.FIELD_SECTION_ID * 4)[0]
    next_offset = struct.unpack_from("<I", pack, (hud.FIELD_SECTION_ID + 1) * 4)[0]
    section = bytearray(pack[section_offset:next_offset])
    section[3] = 1
    base = 0x30000000
    group_count = struct.unpack_from("<I", section, 0x1C)[0]
    group_list = struct.unpack_from("<I", section, 0x20)[0]
    struct.pack_into("<I", section, 0x20, base + group_list)
    for group_id in range(group_count):
        group = group_list + group_id * 0x14
        relative_entries = struct.unpack_from("<I", section, group + 0x10)[0]
        struct.pack_into("<I", section, group + 0x10, base + group + relative_entries)
    return bytes(section)


@unittest.skipUnless(PACK.is_file(), "set FF12_COOP_MRP_PACK for asset tests")
class PartyHudLayoutTests(unittest.TestCase):
    def test_replaced_mrp_section_aborts_before_the_first_layout_write(self) -> None:
        class ReplacedDuringCommit(FakeProcess):
            def __init__(self, section: bytes) -> None:
                super().__init__(section)
                self.section_owner_reads = 0

            def u64(self, address: int) -> int:
                pointer = hud.MRP_SECTION_TABLE + hud.FIELD_SECTION_ID * 8
                if address == pointer:
                    self.section_owner_reads += 1
                    if self.section_owner_reads >= 2:
                        return self.base + 0x100000
                return super().u64(address)

        process = ReplacedDuringCommit(live_field_section())
        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)

        self.assertEqual(scaler.tick(process, now=1.0), "stabilizing")
        self.assertEqual(process.writes, 0)

    def test_production_settle_window_defers_live_mrp_writes(self) -> None:
        process = FakeProcess(live_field_section())
        scaler = hud.PartyHudLayoutScaler(
            retry_seconds=0.05,
            settle_seconds=1.25,
        )

        self.assertEqual(scaler.tick(process, now=10.0), "stabilizing")
        self.assertEqual(process.writes, 0)
        self.assertEqual(scaler.tick(process, now=11.0), "stabilizing")
        self.assertEqual(process.writes, 0)
        self.assertEqual(scaler.tick(process, now=11.25), "applied")
        self.assertGreater(process.writes, 0)

    def test_vanilla_fingerprint_and_reviewed_transform(self) -> None:
        process = FakeProcess(live_field_section())
        fields = hud._collect_fields(process, process.base)
        self.assertEqual(hud._fingerprint(fields), hud.VANILLA_FINGERPRINT)

        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")
        self.assertGreater(process.writes, 0)
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.REDUCED_FINGERPRINT,
        )

        group_list = process.u32(process.base + 0x20)
        group40 = group_list + 40 * 0x14
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group40 + 4, 8)),
            (122, 242, 900, 44),
        )
        self.assertEqual(process.u32(group40 + 0x0C), hud.PARTY_HUD_ROW_GAP)

        expected_sibling_roots = {
            49: (317, 245, 900, 60),
            50: (78, 244, 408, 48),
            51: (470, 236, 1350, 66),
        }
        for group_id, expected in expected_sibling_roots.items():
            group = group_list + group_id * 0x14
            self.assertEqual(
                struct.unpack("<hhhh", process.read(group + 4, 8)),
                expected,
            )
        self.assertEqual(
            process.u32(group_list + 49 * 0x14 + 0x0C),
            hud.PARTY_HUD_ROW_GAP,
        )

        group50 = group_list + hud.ACTION_PANEL_GROUP_ID * 0x14
        action_entry = process.u32(group50 + 0x10)
        action_fields = []
        for entry_id in range(5):
            size = process.read(action_entry, 1)[0]
            action_fields.append(
                struct.unpack("<hhhh", process.read(action_entry + 4, 8))
            )
            action_entry += size
        self.assertEqual(action_fields[0], (183, 20, 140, 17))
        self.assertEqual(action_fields[3], (119, 0, 94, 34))
        self.assertEqual(action_fields[4], (220, 17, 113, 25))

    def test_previous_action_label_shift_migrates_only_the_action_panel(self) -> None:
        process = FakeProcess(live_field_section())
        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")

        fields = hud._collect_fields(process, process.base)
        action_index = 0
        for field in fields:
            if field.group_id == hud.ACTION_PANEL_GROUP_ID:
                process.write(
                    field.address,
                    hud._ACTION_PANEL_PREVIOUS_POSITIONED_VALUES[action_index],
                )
                action_index += 1
        previous_fields = hud._collect_fields(process, process.base)
        self.assertEqual(
            hud._fingerprint(previous_fields),
            hud.PRE_ACTION_BAR_POSITION_POST_PARTY_NAME_FINGERPRINT,
        )
        before = {
            (field.group_id, field.entry_id, field.kind): field.value
            for field in previous_fields
            if field.group_id != hud.ACTION_PANEL_GROUP_ID
        }

        upgraded_scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(upgraded_scaler.tick(process, now=2.0), "applied")
        after_fields = hud._collect_fields(process, process.base)
        self.assertEqual(hud._fingerprint(after_fields), hud.REDUCED_FINGERPRINT)
        after = {
            (field.group_id, field.entry_id, field.kind): field.value
            for field in after_fields
            if field.group_id != hud.ACTION_PANEL_GROUP_ID
        }
        self.assertEqual(after, before)

    def test_second_action_label_shift_migrates_only_the_action_panel(self) -> None:
        process = FakeProcess(live_field_section())
        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")

        fields = hud._collect_fields(process, process.base)
        action_index = 0
        for field in fields:
            if field.group_id == hud.ACTION_PANEL_GROUP_ID:
                process.write(
                    field.address,
                    hud._ACTION_PANEL_SECOND_POSITIONED_VALUES[action_index],
                )
                action_index += 1
        previous_fields = hud._collect_fields(process, process.base)
        self.assertEqual(
            hud._fingerprint(previous_fields),
            hud.PRE_ACTION_BAR_SECOND_POSITION_POST_PARTY_NAME_FINGERPRINT,
        )
        before = {
            (field.group_id, field.entry_id, field.kind): field.value
            for field in previous_fields
            if field.group_id != hud.ACTION_PANEL_GROUP_ID
        }

        upgraded_scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(upgraded_scaler.tick(process, now=2.0), "applied")
        after_fields = hud._collect_fields(process, process.base)
        self.assertEqual(hud._fingerprint(after_fields), hud.REDUCED_FINGERPRINT)
        after = {
            (field.group_id, field.entry_id, field.kind): field.value
            for field in after_fields
            if field.group_id != hud.ACTION_PANEL_GROUP_ID
        }
        self.assertEqual(after, before)

    def test_reduced_layout_uses_a_small_generation_guard_between_loads(self) -> None:
        process = FakeProcess(live_field_section())
        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)

        self.assertEqual(scaler.tick(process, now=1.0), "applied")
        reads_after_apply = process.reads
        self.assertEqual(scaler.tick(process, now=1.01), "reduced")
        self.assertEqual(process.reads, reads_after_apply)

        self.assertEqual(scaler.tick(process, now=1.06), "reduced")
        self.assertLessEqual(process.reads - reads_after_apply, 6)

    def test_same_address_mrp_reload_invalidates_the_small_guard(self) -> None:
        process = FakeProcess(live_field_section())
        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")

        reloaded = FakeProcess(live_field_section(), base=process.base)
        process.memory.update(
            {
                address: value
                for address, value in reloaded.memory.items()
                if address >= process.base
            }
        )
        self.assertEqual(scaler.tick(process, now=1.06), "applied")
        group_list = process.u32(process.base + 0x20)
        self.assertEqual(process.u32(group_list + 50 * 0x14 + 0x0C), 39)
        group21 = group_list + hud.PARTY_SELECTION_PANEL_GROUP_ID * 0x14
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group21 + 4, 8)),
            hud.PARTY_SELECTION_PANEL_REDUCED_RECT,
        )

        # Numeric sprites lose exactly 15% of width/height while retaining
        # their authored X/Y.  The adjacent name entry is untouched by this
        # second-stage scale.
        group44 = group_list + 44 * 0x14
        group44_entry = process.u32(group44 + 0x10)
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group44_entry + 4, 8)),
            (61, 4, 23, 32),
        )
        group47 = group_list + 47 * 0x14
        group47_entry = process.u32(group47 + 0x10)
        group40 = group_list + 40 * 0x14
        group40_entry = process.u32(group40 + 0x10)
        for _entry_id in range(hud.PARTY_NAME_ENTRY_ID):
            group40_entry += process.read(group40_entry, 1)[0]
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group40_entry + 4, 8))[1],
            hud.PARTY_NAME_Y,
        )
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group47_entry + 4, 8)),
            (384, hud.ESPER_NAME_Y, 150, 24),
        )
        self.assertEqual(
            process.read(group47_entry + 0x10, 1),
            bytes((hud.ESPER_NAME_SCALE_VALUE,)),
        )
        group48 = group_list + 48 * 0x14
        group48_entry = process.u32(group48 + 0x10)
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group48_entry + 4, 8)),
            (75, 7, 18, 20),
        )

    def test_legacy_reduced_layout_upgrades_only_missing_siblings(self) -> None:
        process = FakeProcess(live_field_section())
        fields = hud._collect_fields(process, process.base)
        for field in fields:
            if field.group_id < 49:
                process.write(field.address, hud._scaled_field(field))
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.LEGACY_REDUCED_FINGERPRINT,
        )

        group_list = process.u32(process.base + 0x20)
        group40 = group_list + 40 * 0x14
        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group40 + 4, 8)),
            (122, 242, 900, 44),
        )
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.REDUCED_FINGERPRINT,
        )

    def test_reduced_layout_is_idempotent(self) -> None:
        process = FakeProcess(live_field_section())
        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        scaler.tick(process, now=1.0)
        writes = process.writes
        self.assertEqual(scaler.tick(process, now=2.0), "reduced")
        self.assertEqual(process.writes, writes)

    def test_previous_reduced_layout_preserves_selection_companions(self) -> None:
        process = FakeProcess(live_field_section())
        fields = hud._collect_fields(process, process.base)
        for field in fields:
            process.write(field.address, hud._scaled_field(field))
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.PREVIOUS_REDUCED_FINGERPRINT,
        )

        group_list = process.u32(process.base + 0x20)
        group40 = group_list + 40 * 0x14
        group49 = group_list + 49 * 0x14
        group51 = group_list + 51 * 0x14
        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group40 + 4, 8)),
            (122, 242, 900, 44),
        )
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group49 + 4, 8)),
            (317, 245, 900, 60),
        )
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group51 + 4, 8)),
            (470, 236, 1350, 66),
        )
        group50 = group_list + 50 * 0x14
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group50 + 4, 8)),
            (78, 244, 408, 48),
        )

    def test_intermediate_layout_restores_action_clip_bounds(self) -> None:
        process = FakeProcess(live_field_section())
        fields = hud._collect_fields(process, process.base)
        action_index = 0
        for field in fields:
            if field.group_id == hud.ACTION_PANEL_GROUP_ID:
                replacement = hud._ACTION_PANEL_INTERMEDIATE_VALUES[action_index]
                action_index += 1
            else:
                replacement = hud._scaled_field(field)
            process.write(field.address, replacement)
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.INTERMEDIATE_REDUCED_FINGERPRINT,
        )

        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")
        group_list = process.u32(process.base + 0x20)
        group50 = group_list + 50 * 0x14
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group50 + 4, 8)),
            (78, 244, 408, 48),
        )
        group49 = group_list + 49 * 0x14
        group51 = group_list + 51 * 0x14
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group49 + 4, 8)),
            (317, 245, 900, 60),
        )
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group51 + 4, 8)),
            (470, 236, 1350, 66),
        )

    def test_restored_overlays_state_reapplies_selection_layout(self) -> None:
        process = FakeProcess(live_field_section())
        fields = hud._collect_fields(process, process.base)
        action_index = 0
        for field in fields:
            if field.group_id <= 48:
                replacement = hud._scaled_field(field)
            elif field.group_id == hud.ACTION_PANEL_GROUP_ID:
                replacement = hud._ACTION_PANEL_INTERMEDIATE_VALUES[action_index]
                action_index += 1
            else:
                replacement = field.value
            process.write(field.address, replacement)
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.RESTORED_OVERLAYS_FINGERPRINT,
        )

        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.REDUCED_FINGERPRINT,
        )

    def test_pre_aligned_reduced_layout_upgrades_reviewed_roots(self) -> None:
        process = FakeProcess(live_field_section())
        fields = hud._collect_fields(process, process.base)
        action_index = 0
        for field in fields:
            if field.group_id == hud.ACTION_PANEL_GROUP_ID:
                replacement = hud._ACTION_PANEL_REFINED_VALUES[action_index]
                action_index += 1
            else:
                replacement = hud._scaled_field(field)
            process.write(field.address, replacement)
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.PRE_ALIGNED_REDUCED_FINGERPRINT,
        )

        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.REDUCED_FINGERPRINT,
        )

    def test_pre_numeric_layout_upgrades_only_reviewed_digits(self) -> None:
        process = FakeProcess(live_field_section())
        fields = hud._collect_fields(process, process.base)
        for field in fields:
            if field.group_id <= 48:
                replacement = hud._reviewed_root_x(field, hud._scaled_field(field))
            elif field.group_id in (49, 51):
                replacement = hud._reviewed_root_x(field, hud._scaled_field(field))
            else:
                replacement = hud._reviewed_root_x(
                    field, hud._action_panel_field(field)
                )
            process.write(field.address, replacement)
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.PRE_NUMERIC_REDUCED_FINGERPRINT,
        )

        group_list = process.u32(process.base + 0x20)
        group47 = group_list + 47 * 0x14
        group47_entry = process.u32(group47 + 0x10)
        name_before = process.read(group47_entry + 4, 0x0D)

        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.REDUCED_FINGERPRINT,
        )
        expected_name = bytearray(name_before[:0x0C])
        expected_name[2:4] = struct.pack("<h", hud.ESPER_NAME_Y)
        self.assertEqual(process.read(group47_entry + 4, 0x0C), bytes(expected_name))
        self.assertEqual(
            process.read(group47_entry + 0x10, 1),
            bytes((hud.ESPER_NAME_SCALE_VALUE,)),
        )

    def test_pre_numeric_y_layout_moves_digits_without_resizing_again(self) -> None:
        process = FakeProcess(live_field_section())
        fields = hud._collect_fields(process, process.base)
        for field in fields:
            if field.group_id <= 48:
                replacement = hud._reviewed_root_x(field, hud._scaled_field(field))
                replacement = hud._numeric_sprite_size(field, replacement)
            elif field.group_id in (49, 51):
                replacement = hud._reviewed_root_x(field, hud._scaled_field(field))
            else:
                replacement = hud._reviewed_root_x(
                    field, hud._action_panel_field(field)
                )
            process.write(field.address, replacement)
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.PRE_NUMERIC_Y_REDUCED_FINGERPRINT,
        )

        group_list = process.u32(process.base + 0x20)
        group44 = group_list + 44 * 0x14
        group44_entry = process.u32(group44 + 0x10)
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group44_entry + 4, 8)),
            (61, 0, 23, 32),
        )

        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group44_entry + 4, 8)),
            (61, 4, 23, 32),
        )

    def test_pre_collision_layout_migrates_without_rescaling(self) -> None:
        process = FakeProcess(live_field_section())
        fields = hud._collect_fields(process, process.base)
        action_index = 0
        for field in fields:
            if field.group_id <= 48:
                replacement = hud._reviewed_root_x(field, hud._scaled_field(field))
                replacement = hud._numeric_sprite_size(field, replacement)
                replacement = hud._numeric_sprite_y(field, replacement)
            elif field.group_id in (49, 51):
                replacement = hud._reviewed_root_x(field, hud._scaled_field(field))
            else:
                replacement = hud._ACTION_PANEL_ALIGNED_VALUES[action_index]
                action_index += 1
                replacement = hud._reviewed_root_x(field, replacement)
            process.write(field.address, replacement)
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.PRE_COLLISION_REDUCED_FINGERPRINT,
        )

        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.REDUCED_FINGERPRINT,
        )

        group_list = process.u32(process.base + 0x20)
        self.assertEqual(
            process.u32(group_list + 40 * 0x14 + 0x0C),
            hud.PARTY_HUD_ROW_GAP,
        )
        group50 = group_list + hud.ACTION_PANEL_GROUP_ID * 0x14
        action_entry = process.u32(group50 + 0x10)
        self.assertEqual(
            struct.unpack("<hhhh", process.read(action_entry + 4, 8)),
            (183, 20, 140, 17),
        )

    def test_previous_hud_refinement_migrates_without_double_scaling(self) -> None:
        process = FakeProcess(live_field_section())
        fields = hud._collect_fields(process, process.base)
        action_index = 0
        for field in fields:
            if field.group_id <= 48:
                replacement = hud._reviewed_root_x(field, hud._scaled_field(field))
                replacement = hud._reviewed_party_row_gap(field, replacement)
                replacement = hud._numeric_sprite_size(field, replacement)
                replacement = hud._numeric_sprite_y(field, replacement)
            elif field.group_id in (49, 51):
                replacement = hud._reviewed_root_x(field, hud._scaled_field(field))
                replacement = hud._reviewed_party_row_gap(field, replacement)
            else:
                replacement = hud._ACTION_PANEL_COLLISION_SAFE_ALIGNED_VALUES[
                    action_index
                ]
                action_index += 1
            process.write(field.address, replacement)
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.PRE_HUD_REFINEMENT_FINGERPRINT,
        )

        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "applied")
        self.assertEqual(
            hud._fingerprint(hud._collect_fields(process, process.base)),
            hud.REDUCED_FINGERPRINT,
        )

        group_list = process.u32(process.base + 0x20)
        group44_entry = process.u32(group_list + 44 * 0x14 + 0x10)
        self.assertEqual(
            struct.unpack("<hhhh", process.read(group44_entry + 4, 8)),
            (61, 4, 23, 32),
        )
        group47_entry = process.u32(group_list + 47 * 0x14 + 0x10)
        self.assertEqual(
            process.read(group47_entry + 0x10, 1),
            bytes((hud.ESPER_NAME_SCALE_VALUE,)),
        )

    def test_foreign_selection_panel_fails_closed(self) -> None:
        process = FakeProcess(live_field_section())
        panel_address = hud._group_rect_address(
            process, process.base, hud.PARTY_SELECTION_PANEL_GROUP_ID
        )
        process.write(panel_address, struct.pack("<h", 571))
        writes = process.writes

        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "foreign")
        self.assertEqual(process.writes, writes)

    def test_overlapping_layout_mod_fails_closed(self) -> None:
        process = FakeProcess(live_field_section())
        group_list = process.u32(process.base + 0x20)
        group40 = group_list + 40 * 0x14
        process.write(group40 + 4, struct.pack("<h", 203))
        writes = process.writes
        scaler = hud.PartyHudLayoutScaler(retry_seconds=0.05)
        self.assertEqual(scaler.tick(process, now=1.0), "foreign")
        self.assertEqual(process.writes, writes)


if __name__ == "__main__":
    unittest.main()
