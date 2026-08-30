from __future__ import annotations

import struct
import unittest

import battle_pack_runtime_catalog as runtime_catalog
from headless_menu_model import MenuMode, P2MenuModel, TargetGroup
from battle_pack_runtime_catalog import (
    BattlePackGameplayCatalog,
    BattlePackRuntimeCatalog,
    LiveCatalogState,
)
from native_action_catalog import NativeActionCatalog


def bits(size: int, *indices: int) -> bytes:
    raw = bytearray(size)
    for index in indices:
        raw[index // 8] |= 1 << (index % 8)
    return bytes(raw)


class BattlePackCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.static = BattlePackGameplayCatalog()
        cls.runtime = BattlePackRuntimeCatalog(NativeActionCatalog())

    def test_native_table_sizes_and_links(self) -> None:
        self.assertEqual(len(self.static.items), 64)
        self.assertEqual(len(self.static.magicks), 81)
        self.assertEqual(len(self.static.technicks), 24)
        self.assertEqual(self.static.items[0].action_id, 0x52)
        self.assertEqual(self.static.magicks[0].action_id, 0x00)
        self.assertEqual(self.static.technicks[11].action_id, 0xA9)

    def test_guest_fixed_actions_come_from_party_member_table(self) -> None:
        lamont = self.static.fixed_member_actions(10)
        vossler = self.static.fixed_member_actions(12)
        reddas = self.static.fixed_member_actions(14)

        self.assertIsNone(self.static.fixed_member_actions(5))
        self.assertIsNotNone(lamont)
        self.assertIsNotNone(vossler)
        self.assertIsNotNone(reddas)
        assert lamont is not None and vossler is not None and reddas is not None
        self.assertEqual(
            [self.runtime.action_catalog.name(action_id) for action_id in lamont[0]],
            ["Cure", "Cura", "Poisona", "Blindna", "Vox", "Aqua", "Balance"],
        )
        self.assertEqual(
            [self.runtime.action_catalog.name(action_id) for action_id in lamont[1]],
            ["First Aid", "Shades of Black"],
        )
        self.assertEqual(
            [self.runtime.action_catalog.name(action_id) for action_id in vossler[1]],
            ["Wither", "Expose", "Souleater", "Telekinesis", "Traveler"],
        )
        self.assertEqual(
            [self.runtime.action_catalog.name(action_id) for action_id in reddas[1]],
            ["Bonecrusher", "Stamp", "Sight Unseeing", "1000 Needles"],
        )

    def test_guest_pages_ignore_main_party_ownership_and_license_bits(self) -> None:
        catalog = BattlePackRuntimeCatalog(
            NativeActionCatalog(),
            party_member_id=13,
            gameplay_catalog=self.static,
        )
        state = LiveCatalogState(
            menu_flags=0,
            acquired_license_bits=bytes(46),
            item_counts={},
            magick_bits=bytes(12),
            technick_bits=bytes(4),
            mist_bits=bits(4, 0, 18),
            signature=b"larsa-fixed-actions",
            magick_license_flags=bytes(0x17),
            current_mp=999,
        )

        pages = catalog.build_pages(state)
        root = [entry.name for entry in pages["root"].entries]
        guest_actions = {
            entry.name
            for key, page in pages.items()
            if key.startswith("magick:") or key == "technicks"
            for entry in page.entries
            if entry.action is not None
        }

        self.assertIn("Magicks & Technicks", root)
        self.assertEqual(
            guest_actions,
            {
                "First Aid",
                "Shades of Black",
                "Cure",
                "Cura",
                "Poisona",
                "Blindna",
                "Vox",
                "Aqua",
                "Balance",
                "Bubble",
            },
        )
        self.assertNotIn("mist", pages)

    def test_dismiss_only_appears_while_an_esper_is_active(self) -> None:
        state = LiveCatalogState(
            menu_flags=1 << 7,
            acquired_license_bits=bytes(46),
            item_counts={},
            magick_bits=bytes(12),
            technick_bits=bytes(4),
            mist_bits=bytes(4),
            signature=b"dismiss",
        )
        normal = self.runtime.build_pages(state)["root"].entries
        summoned = self.runtime.build_pages(
            state,
            summon_active=True,
        )["root"].entries
        self.assertNotIn("Dismiss", (entry.name for entry in normal))
        dismiss = next(entry for entry in summoned if entry.name == "Dismiss")
        self.assertEqual(dismiss.action.action_id, 0x113)
        self.assertEqual(dismiss.description, "Dismiss the summoned Esper.")
        self.assertTrue(dismiss.action.self_only)
        self.assertNotIn("Mist", (entry.name for entry in summoned))

        normal_names = tuple(entry.name for entry in normal)
        summoned_names = tuple(entry.name for entry in summoned)
        self.assertEqual(summoned_names.index("Dismiss"), normal_names.index("Mist"))

    def test_dismiss_submits_immediately_to_the_summoner(self) -> None:
        state = LiveCatalogState(
            menu_flags=1 << 7,
            acquired_license_bits=bytes(46),
            item_counts={},
            magick_bits=bytes(12),
            technick_bits=bytes(4),
            mist_bits=bytes(4),
            signature=b"dismiss-submit",
        )
        model = P2MenuModel(
            issuer=0x100022,
            pages=self.runtime.build_pages(state, summon_active=True),
        )
        model.open()
        model.navigate(1)

        event = model.confirm()

        self.assertEqual(event.kind, "submit")
        self.assertEqual(event.request.issuer, 0x100022)
        self.assertEqual(event.request.target, 0x100022)
        self.assertEqual(event.request.action_id, 0x113)
        self.assertEqual(model.mode, MenuMode.CLOSED)

    def test_item_terminal_and_display_ids_are_kept_separate(self) -> None:
        dispel = self.static.items[53]
        warp = self.static.items[55]
        self.assertEqual(dispel.action_id, 0x87)
        self.assertEqual(dispel.presentation_action_id, 0x8C)
        self.assertEqual(warp.action_id, 0x89)
        self.assertEqual(warp.presentation_action_id, 0x87)

        state = LiveCatalogState(
            menu_flags=0,
            acquired_license_bits=bytes(46),
            item_counts={0x87: 3, 0x89: 2},
            magick_bits=bytes(12),
            technick_bits=bytes(4),
            mist_bits=bytes(4),
            signature=b"mote-display-terminal-split",
        )
        entries = self.runtime.build_pages(state)["items"].entries
        by_name = {entry.name: entry for entry in entries}
        self.assertEqual(by_name["Dispel Mote"].action.action_id, 0x87)
        self.assertEqual(by_name["Dispel Mote"].action.quantity, 3)
        self.assertEqual(by_name["Warp Mote"].action.action_id, 0x89)
        self.assertEqual(by_name["Warp Mote"].action.quantity, 2)

    def test_items_follow_the_games_saved_manual_order(self) -> None:
        state = LiveCatalogState(
            menu_flags=0,
            acquired_license_bits=bytes(46),
            item_counts={0x52: 3, 0x5B: 1, 0x62: 2},
            magick_bits=bytes(12),
            technick_bits=bytes(4),
            mist_bits=bytes(4),
            signature=b"manual-item-order",
            item_order=(9, 16, 0),
        )
        entries = self.runtime.build_pages(state)["items"].entries
        self.assertEqual(
            [entry.action.action_id for entry in entries],
            [0x5B, 0x62, 0x52],
        )

    def test_incomplete_manual_item_order_falls_back_to_auto_sort(self) -> None:
        records = [self.static.items[9], self.static.items[0]]
        auto_sorted = sorted(
            records,
            key=lambda record: (record.sort_order, record.content_index),
        )
        result = runtime_catalog.records_in_native_item_order(
            auto_sorted,
            (9,),
        )
        self.assertEqual(result, tuple(auto_sorted))

    def test_native_item_order_reader_rejects_a_torn_owner(self) -> None:
        owner = 0x300000
        order = 0x400000

        class FakeProcess:
            def __init__(self) -> None:
                self.owner_reads = 0

            def u64(self, address: int) -> int:
                if address == runtime_catalog.NATIVE_SORT_OWNER_GLOBAL:
                    self.owner_reads += 1
                    return owner if self.owner_reads == 1 else owner + 8
                if address == runtime_catalog.NATIVE_RESOURCE_BASE_GLOBAL:
                    return 0
                return 0

            def u32(self, address: int) -> int:
                if address == runtime_catalog.NATIVE_ITEM_ORDER_POINTER_GLOBAL:
                    return order
                if address == owner + runtime_catalog.NATIVE_ITEM_ORDER_COUNT_OFFSET:
                    return 3
                return 0

            def read(self, address: int, size: int) -> bytes:
                if (address, size) == (order, 6):
                    return struct.pack("<3H", 9, 16, 0)
                raise OSError(address)

        self.assertIsNone(runtime_catalog.read_native_item_order(FakeProcess()))

    def test_three_players_share_every_global_catalog_payload(self) -> None:
        owner = 0x100000
        battle = 0x300000
        info = owner + runtime_catalog.GLOBAL_BATTLE_INFO_OFFSET
        reads: dict[tuple[int, int], int] = {}
        counters = bytearray(runtime_catalog.ITEM_COUNTER_COUNT * 2)
        struct.pack_into("<H", counters, 0, 7)

        class FakeProcess:
            def u64(self, address: int) -> int:
                return {
                    runtime_catalog.MAIN_SAVE_OWNER_GLOBAL: owner,
                    runtime_catalog.BATTLE_SAVE_BASE_GLOBAL: battle,
                }.get(address, 0)

            def u32(self, _address: int) -> int:
                return 0

            def read(self, address: int, size: int) -> bytes:
                key = (address, size)
                reads[key] = reads.get(key, 0) + 1
                if key == (info + runtime_catalog.MENU_FLAGS_OFFSET, 4):
                    return struct.pack("<I", 1)
                if key == (
                    battle + runtime_catalog.ITEM_COUNTERS_OFFSET,
                    runtime_catalog.ITEM_COUNTER_COUNT * 2,
                ):
                    return bytes(counters)
                if key == (
                    battle + runtime_catalog.MAGICK_OWNERSHIP_OFFSET,
                    runtime_catalog.MAGICK_OWNERSHIP_SIZE,
                ):
                    return bits(runtime_catalog.MAGICK_OWNERSHIP_SIZE, 0)
                return bytes(size)

        process = FakeProcess()
        catalogs = tuple(
            BattlePackRuntimeCatalog(
                NativeActionCatalog(),
                party_member_id=member_id,
                gameplay_catalog=self.static,
            )
            for member_id in (0, 3, 5)
        )
        shared = catalogs[0].read_shared_state(process)
        self.assertIsNotNone(shared)
        assert shared is not None
        states = tuple(
            catalog.read_player_state(process, shared) for catalog in catalogs
        )

        self.assertTrue(all(state is not None for state in states))
        for address, size in (
            (info + runtime_catalog.MENU_FLAGS_OFFSET, 4),
            (
                battle + runtime_catalog.ITEM_COUNTERS_OFFSET,
                runtime_catalog.ITEM_COUNTER_COUNT * 2,
            ),
            (
                battle + runtime_catalog.MAGICK_OWNERSHIP_OFFSET,
                runtime_catalog.MAGICK_OWNERSHIP_SIZE,
            ),
            (
                battle + runtime_catalog.TECHNICK_OWNERSHIP_OFFSET,
                runtime_catalog.TECHNICK_OWNERSHIP_SIZE,
            ),
            (
                battle + runtime_catalog.MIST_OWNERSHIP_OFFSET,
                runtime_catalog.MIST_OWNERSHIP_SIZE,
            ),
        ):
            self.assertEqual(reads[(address, size)], 1)
        self.assertIs(states[0].item_counts, states[1].item_counts)
        self.assertIs(states[1].item_counts, states[2].item_counts)

    def test_target_rules_come_from_action_flags(self) -> None:
        steal = self.static.rules(0xA9)
        first_aid = self.static.rules(0x9E)
        charge = self.static.rules(0xA3)
        potion = self.static.rules(0x52)
        dark_mote = self.static.rules(0x7D)
        warp_mote = self.static.rules(0x89)
        float_mote = self.static.rules(0x8E)
        cure = self.static.rules(0x00)
        self.assertEqual(steal.target_groups, (TargetGroup.FOES,))
        self.assertEqual(steal.default_group, TargetGroup.FOES)
        self.assertEqual(
            first_aid.target_groups,
            (
                TargetGroup.PARTY,
                TargetGroup.NEUTRALS,
            ),
        )
        self.assertEqual(first_aid.default_group, TargetGroup.PARTY)
        self.assertTrue(charge.sole_self)
        self.assertEqual(potion.default_group, TargetGroup.PARTY)
        self.assertEqual(cure.default_group, TargetGroup.PARTY)
        self.assertIn(TargetGroup.RESERVE, potion.target_groups)
        self.assertIn(TargetGroup.RESERVE, cure.target_groups)
        self.assertNotIn(TargetGroup.RESERVE, first_aid.target_groups)
        self.assertEqual(dark_mote.default_group, TargetGroup.FOES)
        self.assertEqual(
            dark_mote.target_groups,
            (
                TargetGroup.FOES,
                TargetGroup.NEUTRALS,
                TargetGroup.PARTY,
            ),
        )
        self.assertEqual(warp_mote.target_groups, (TargetGroup.FOES,))
        self.assertEqual(
            float_mote.target_groups,
            (TargetGroup.PARTY, TargetGroup.NEUTRALS),
        )

    def test_every_playable_aoe_item_keeps_its_native_radius(self) -> None:
        expected = {
            0x63: 6.0,  # Soleil Fang
            0x67: 10.0,  # Megalixir
            0x6A: 100.0,  # Dark Energy
            0x7D: 8.0,  # Dark Mote
            0x83: 8.0,  # Scathe Mote terminal action
            0x86: 10.0,  # Cura Mote terminal action
            0x89: 8.0,  # Warp Mote terminal action
            0x8B: 10.0,  # Hastega Mote terminal action
            0x90: 20.0,  # Dark Matter
        }
        for action_id, radius in expected.items():
            with self.subTest(action_id=action_id):
                self.assertEqual(
                    self.runtime._definition(action_id).area_radius,
                    radius,
                )

    def test_native_area_radius_is_carried_into_action_definition(self) -> None:
        # Section-14 byte +0x06 is zero for Fire's single-target form and the
        # exact world radius used by the native large circle for Fira/Cura.
        self.assertEqual(self.static.rules(0x12).area_radius, 0.0)  # Fire
        self.assertEqual(self.static.rules(0x17).area_radius, 8.0)  # Fira
        self.assertEqual(self.static.rules(0x04).area_radius, 10.0)  # Cura
        self.assertEqual(self.runtime._definition(0x17).area_radius, 8.0)

    def test_playable_quickenings_are_single_target_even_with_presentation_radius(
        self,
    ) -> None:
        for action_id in runtime_catalog.CHARACTER_QUICKENING_ACTION_IDS:
            with self.subTest(action_id=action_id):
                self.assertEqual(self.runtime._definition(action_id).area_radius, 0.0)

    def test_complete_tree_has_locked_school_mist_and_summon(self) -> None:
        # License 0 = one Quickening; 18 = Belias; 182 grants Cure/Blindna;
        # 289 is Steal in the native license table.
        licenses = bits(46, 0, 18, 182, 289)
        state = LiveCatalogState(
            menu_flags=(
                (1 << 0)  # Magicks & Technicks
                | (1 << 1)  # White
                | (1 << 2)  # Black
                | (1 << 6)  # Technicks
                | (1 << 7)  # Quickening
                | (1 << 8)  # Summon
            ),
            acquired_license_bits=licenses,
            item_counts={0x52: 3, 0x5B: 1},
            magick_bits=bits(12, 0, 18),  # Cure and Fire owned
            technick_bits=bits(4, 11),  # Steal owned
            mist_bits=bits(4, 15, 18),  # Intercession and Belias obtained
            signature=b"test",
        )
        pages = self.runtime.build_pages(state)
        self.assertIn("mist", pages)
        self.assertEqual(
            [entry.name for entry in pages["quickenings"].entries],
            ["Intercession"],
        )
        self.assertEqual([entry.name for entry in pages["summons"].entries], ["Belias"])
        categories = pages["magicks"].entries
        self.assertEqual(
            [entry.name for entry in categories[:2]], ["White Magicks", "Black Magicks"]
        )
        self.assertTrue(categories[0].enabled)
        self.assertFalse(categories[1].enabled)

    def test_each_main_character_uses_its_native_quickening_range(self) -> None:
        first_action_by_member = {
            0: 0xD0,  # Vaan: Red Spiral
            1: 0xDC,  # Ashe: Nordswain's Glow
            2: 0xD3,  # Fran: Feral Strike
            3: 0xD6,  # Balthier: Fires of War
            4: 0xD9,  # Basch: Fulminating Darkness
            5: 0xDF,  # Penelo: Intercession
        }
        state = LiveCatalogState(
            menu_flags=0,
            acquired_license_bits=bits(46, 0),
            item_counts={},
            magick_bits=bytes(12),
            technick_bits=bytes(4),
            mist_bits=bytes(4),
            signature=b"live-first-quickening",
        )
        for member_id, expected_action_id in first_action_by_member.items():
            with self.subTest(member_id=member_id):
                catalog = BattlePackRuntimeCatalog(
                    NativeActionCatalog(), party_member_id=member_id
                )
                entries = catalog.build_pages(state)["quickenings"].entries
                self.assertEqual(
                    [entry.action.action_id for entry in entries],
                    [expected_action_id],
                )

    def test_live_actor_license_block_updates_mist_without_reload(self) -> None:
        owner = 0x100000
        battle = 0x300000
        actor = 0x500000
        battle_keep = 0x600000
        info = owner + runtime_catalog.GLOBAL_BATTLE_INFO_OFFSET
        member = (
            info
            + runtime_catalog.PARTY_MEMBER_BASE_OFFSET
            + runtime_catalog.PENELO_PARTY_MEMBER_ID
            * runtime_catalog.PARTY_MEMBER_STRIDE
        )
        persistent_licenses = bytes(runtime_catalog.PARTY_LICENSES_SIZE)
        live_licenses = bits(runtime_catalog.PARTY_LICENSES_SIZE, 0)
        payloads = {
            (info + runtime_catalog.MENU_FLAGS_OFFSET, 4): struct.pack("<I", 0),
            (
                member + runtime_catalog.PARTY_LICENSES_OFFSET,
                runtime_catalog.PARTY_LICENSES_SIZE,
            ): persistent_licenses,
            (
                battle_keep + runtime_catalog.PARTY_LICENSES_OFFSET,
                runtime_catalog.PARTY_LICENSES_SIZE,
            ): live_licenses,
            (battle_keep, 1): b"\x04",
            (battle_keep + 0x4C, 2): struct.pack("<H", 45),
            (
                battle_keep + runtime_catalog.MAGICK_LICENSE_FLAGS_OFFSET,
                runtime_catalog.MAGICK_LICENSE_FLAGS_SIZE,
            ): bytes(runtime_catalog.MAGICK_LICENSE_FLAGS_SIZE),
        }

        class FakeProcess:
            def u64(self, address: int) -> int:
                return {
                    runtime_catalog.MAIN_SAVE_OWNER_GLOBAL: owner,
                    runtime_catalog.BATTLE_SAVE_BASE_GLOBAL: battle,
                    actor + 0x698: battle_keep,
                }.get(address, 0)

            def read(self, address: int, size: int) -> bytes:
                return payloads.get((address, size), bytes(size))

        state = self.runtime.read_state(FakeProcess(), live_actor=actor)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.acquired_license_bits, live_licenses)
        self.assertNotEqual(state.acquired_license_bits, persistent_licenses)
        entries = self.runtime.build_pages(state)["quickenings"].entries
        self.assertEqual(
            [entry.action.action_id for entry in entries],
            [0xDF],
        )

    def test_failed_live_refresh_retains_the_previously_published_generation(
        self,
    ) -> None:
        owner = 0x100000
        battle = 0x300000
        actor = 0x500000
        battle_keep = 0x600000
        info = owner + runtime_catalog.GLOBAL_BATTLE_INFO_OFFSET
        member = (
            info
            + runtime_catalog.PARTY_MEMBER_BASE_OFFSET
            + runtime_catalog.PENELO_PARTY_MEMBER_ID
            * runtime_catalog.PARTY_MEMBER_STRIDE
        )
        persistent_licenses = bits(runtime_catalog.PARTY_LICENSES_SIZE, 18)
        live_licenses = bits(runtime_catalog.PARTY_LICENSES_SIZE, 0)
        persistent_flags = bytes((0x5A,)) * runtime_catalog.MAGICK_LICENSE_FLAGS_SIZE
        live_flags_address = battle_keep + runtime_catalog.MAGICK_LICENSE_FLAGS_OFFSET
        payloads = {
            (info + runtime_catalog.MENU_FLAGS_OFFSET, 4): struct.pack("<I", 0),
            (
                member + runtime_catalog.PARTY_LICENSES_OFFSET,
                runtime_catalog.PARTY_LICENSES_SIZE,
            ): persistent_licenses,
            (member, 1): b"\x04",
            (member + 0x4C, 2): struct.pack("<H", 17),
            (
                member + runtime_catalog.MAGICK_LICENSE_FLAGS_OFFSET,
                runtime_catalog.MAGICK_LICENSE_FLAGS_SIZE,
            ): persistent_flags,
            (
                battle_keep + runtime_catalog.PARTY_LICENSES_OFFSET,
                runtime_catalog.PARTY_LICENSES_SIZE,
            ): live_licenses,
            (battle_keep, 1): b"\x00",
            (battle_keep + 0x4C, 2): struct.pack("<H", 99),
        }

        class FailingLiveProcess:
            def u64(self, address: int) -> int:
                return {
                    runtime_catalog.MAIN_SAVE_OWNER_GLOBAL: owner,
                    runtime_catalog.BATTLE_SAVE_BASE_GLOBAL: battle,
                    actor + 0x698: battle_keep,
                }.get(address, 0)

            def read(self, address: int, size: int) -> bytes:
                if address == live_flags_address:
                    raise OSError("battle-keep generation changed")
                return payloads.get((address, size), bytes(size))

        state = self.runtime.read_state(FailingLiveProcess(), live_actor=actor)

        self.assertIsNone(state)

    def test_changed_live_battle_keep_generation_is_never_published(self) -> None:
        owner = 0x100000
        battle = 0x300000
        actor = 0x500000
        first_battle_keep = 0x600000
        second_battle_keep = 0x700000
        info = owner + runtime_catalog.GLOBAL_BATTLE_INFO_OFFSET
        member = (
            info
            + runtime_catalog.PARTY_MEMBER_BASE_OFFSET
            + runtime_catalog.PENELO_PARTY_MEMBER_ID
            * runtime_catalog.PARTY_MEMBER_STRIDE
        )
        live_licenses = bits(runtime_catalog.PARTY_LICENSES_SIZE, 0)
        payloads = {
            (info + runtime_catalog.MENU_FLAGS_OFFSET, 4): struct.pack("<I", 0),
            (
                member + runtime_catalog.PARTY_LICENSES_OFFSET,
                runtime_catalog.PARTY_LICENSES_SIZE,
            ): bytes(runtime_catalog.PARTY_LICENSES_SIZE),
            (member, 1): b"\x04",
            (member + 0x4C, 2): struct.pack("<H", 17),
            (
                member + runtime_catalog.MAGICK_LICENSE_FLAGS_OFFSET,
                runtime_catalog.MAGICK_LICENSE_FLAGS_SIZE,
            ): bytes(runtime_catalog.MAGICK_LICENSE_FLAGS_SIZE),
            (
                first_battle_keep + runtime_catalog.PARTY_LICENSES_OFFSET,
                runtime_catalog.PARTY_LICENSES_SIZE,
            ): live_licenses,
            (first_battle_keep, 1): b"\x04",
            (first_battle_keep + 0x4C, 2): struct.pack("<H", 99),
            (
                first_battle_keep + runtime_catalog.MAGICK_LICENSE_FLAGS_OFFSET,
                runtime_catalog.MAGICK_LICENSE_FLAGS_SIZE,
            ): bytes(runtime_catalog.MAGICK_LICENSE_FLAGS_SIZE),
        }

        class ChangingGenerationProcess:
            battle_keep_reads = 0

            def u64(self, address: int) -> int:
                if address == runtime_catalog.MAIN_SAVE_OWNER_GLOBAL:
                    return owner
                if address == runtime_catalog.BATTLE_SAVE_BASE_GLOBAL:
                    return battle
                if address == actor + 0x698:
                    self.battle_keep_reads += 1
                    return (
                        first_battle_keep
                        if self.battle_keep_reads == 1
                        else second_battle_keep
                    )
                return 0

            def read(self, address: int, size: int) -> bytes:
                return payloads.get((address, size), bytes(size))

        process = ChangingGenerationProcess()
        self.assertIsNone(self.runtime.read_state(process, live_actor=actor))
        self.assertEqual(process.battle_keep_reads, 2)

    def test_changed_global_owner_or_battle_generation_is_never_published(self) -> None:
        owner = 0x100000
        battle = 0x300000

        for changed_global in ("owner", "battle"):
            with self.subTest(changed_global=changed_global):

                class ChangingGlobalProcess:
                    owner_reads = 0
                    battle_reads = 0

                    def u64(self, address: int) -> int:
                        if address == runtime_catalog.MAIN_SAVE_OWNER_GLOBAL:
                            self.owner_reads += 1
                            return owner + int(
                                changed_global == "owner" and self.owner_reads > 1
                            )
                        if address == runtime_catalog.BATTLE_SAVE_BASE_GLOBAL:
                            self.battle_reads += 1
                            return battle + int(
                                changed_global == "battle" and self.battle_reads > 1
                            )
                        return 0

                    def read(self, address: int, size: int) -> bytes:
                        return bytes(size)

                self.assertIsNone(self.runtime.read_state(ChangingGlobalProcess()))

    def test_changed_live_actor_handle_generation_is_never_published(self) -> None:
        owner = 0x100000
        battle = 0x300000
        actor = 0x500000
        battle_keep = 0x600000

        class ChangingHandleProcess:
            handle_reads = 0

            def u64(self, address: int) -> int:
                return {
                    runtime_catalog.MAIN_SAVE_OWNER_GLOBAL: owner,
                    runtime_catalog.BATTLE_SAVE_BASE_GLOBAL: battle,
                    actor + 0x698: battle_keep,
                }.get(address, 0)

            def read(self, address: int, size: int) -> bytes:
                if (address, size) == (actor + 0x08, 4):
                    self.handle_reads += 1
                    return struct.pack("<I", 0x100001 + self.handle_reads)
                return bytes(size)

        process = ChangingHandleProcess()
        self.assertIsNone(self.runtime.read_state(process, live_actor=actor))
        self.assertEqual(process.handle_reads, 2)

    def test_locked_category_is_navigable_but_not_openable(self) -> None:
        state = LiveCatalogState(
            menu_flags=(1 << 0) | (1 << 1) | (1 << 2),
            acquired_license_bits=bits(46, 182),
            item_counts={},
            magick_bits=bits(12, 0, 18),
            technick_bits=bytes(4),
            mist_bits=bytes(4),
            signature=b"test2",
        )
        model = P2MenuModel(0x100008, self.runtime.build_pages(state))
        model.open()
        model.navigate(1)
        model.confirm()
        self.assertEqual(model.page.key, "magicks")
        model.navigate(1)
        self.assertEqual(model.selected_entry.name, "Black Magicks")
        self.assertFalse(model.selected_entry.enabled)
        event = model.confirm()
        self.assertEqual(event.kind, "ignored")
        self.assertEqual(model.mode, MenuMode.LIST)
        self.assertEqual(model.page.key, "magicks")

    def test_root_magicks_opens_even_when_every_school_is_locked(self) -> None:
        state = LiveCatalogState(
            menu_flags=(1 << 0) | (1 << 1) | (1 << 2),
            acquired_license_bits=bytes(46),
            item_counts={},
            magick_bits=bits(12, 0, 18),
            technick_bits=bytes(4),
            mist_bits=bytes(4),
            signature=b"all-locked",
        )
        pages = self.runtime.build_pages(state)
        root_magicks = pages["root"].entries[1]
        self.assertEqual(root_magicks.name, "Magicks & Technicks")
        self.assertTrue(root_magicks.enabled)

        model = P2MenuModel(0x100008, pages)
        model.open()
        model.navigate(1)
        event = model.confirm()
        self.assertEqual(event.kind, "page_opened")
        self.assertEqual(model.page.key, "magicks")
        self.assertFalse(model.selected_entry.enabled)

    def test_live_summary_flags_unlock_new_license_before_save_mirror(self) -> None:
        # +1AA bit 6/7 are White Magick 1/2.  The stale acquired-node mirror
        # contains only White 1, while the live actor already owns White 2.
        state = LiveCatalogState(
            menu_flags=(1 << 0) | (1 << 1),
            acquired_license_bits=bits(46, 182),
            item_counts={},
            magick_bits=bits(12, 0, 1, 2, 3),
            technick_bits=bytes(4),
            mist_bits=bytes(4),
            signature=b"live-white-2",
            magick_license_flags=bits(0x17, 6, 7),
        )
        pages = self.runtime.build_pages(state)
        self.assertEqual(
            [entry.name for entry in pages["magick:white_magicks"].entries],
            ["Cure", "Blindna", "Vox", "Poisona"],
        )

    def test_all_school_rows_and_technicks_remain_visible_when_locked(self) -> None:
        state = LiveCatalogState(
            menu_flags=1,
            acquired_license_bits=bytes(46),
            item_counts={},
            magick_bits=bytes(12),
            technick_bits=bytes(4),
            mist_bits=bytes(4),
            signature=b"all-school-rows",
            magick_license_flags=bytes(0x17),
        )
        pages = self.runtime.build_pages(state)
        self.assertEqual(
            [entry.name for entry in pages["magicks"].entries],
            [
                "White Magicks",
                "Black Magicks",
                "Time Magicks",
                "Green Magicks",
                "Arcane Magicks",
                "Technicks",
            ],
        )
        self.assertTrue(all(not entry.enabled for entry in pages["magicks"].entries))

    def test_magick_disables_below_mp_cost_and_reenables_at_threshold(self) -> None:
        common = dict(
            menu_flags=(1 << 0) | (1 << 1),
            acquired_license_bits=bits(46, 182),
            item_counts={},
            magick_bits=bits(12, 0),
            technick_bits=bytes(4),
            mist_bits=bytes(4),
            magick_license_flags=bits(0x17, 6),
        )
        low = LiveCatalogState(
            **common,
            signature=b"mp-7",
            current_mp=7,
        )
        enough = LiveCatalogState(
            **common,
            signature=b"mp-8",
            current_mp=8,
        )

        low_cure = self.runtime.build_pages(low)["magick:white_magicks"].entries[0]
        enough_cure = self.runtime.build_pages(enough)["magick:white_magicks"].entries[
            0
        ]
        self.assertEqual(low_cure.action.mp_cost, 8)
        self.assertFalse(low_cure.enabled)
        self.assertFalse(low_cure.action.enabled)
        self.assertTrue(enough_cure.enabled)
        self.assertTrue(enough_cure.action.enabled)

    def _restriction_state(self, **changes) -> LiveCatalogState:
        common = dict(
            menu_flags=(1 << 0) | (1 << 6),
            acquired_license_bits=bits(46, 0, 18, 182, 276),
            item_counts={0x52: 3},
            magick_bits=bits(12, 0),
            technick_bits=bits(4, 11),
            mist_bits=bits(4, 15, 18),
            signature=b"command-restrictions",
            magick_license_flags=bits(0x17, 6),
            current_mp=99,
        )
        common.update(changes)
        return LiveCatalogState(**common)

    def test_silence_disables_magicks_but_keeps_technicks(self) -> None:
        pages = self.runtime.build_pages(
            self._restriction_state(status_mask=runtime_catalog.SILENCE_STATUS_MASK)
        )

        self.assertFalse(pages["magick:white_magicks"].entries[0].enabled)
        self.assertTrue(pages["technicks"].entries[0].enabled)
        magicks = {entry.name: entry for entry in pages["magicks"].entries}
        self.assertFalse(magicks["White Magicks"].enabled)
        self.assertTrue(magicks["Technicks"].enabled)
        self.assertTrue(
            next(
                entry
                for entry in pages["root"].entries
                if entry.name == "Magicks & Technicks"
            ).enabled
        )

    def test_native_command_blocking_statuses_disable_every_root_option(self) -> None:
        for bit in (0, 1, 3, 4, 5, 13, 31):
            with self.subTest(status_bit=bit):
                pages = self.runtime.build_pages(
                    self._restriction_state(status_mask=1 << bit)
                )
                self.assertTrue(
                    all(not entry.enabled for entry in pages["root"].entries)
                )
                self.assertFalse(pages["technicks"].entries[0].action.enabled)
                self.assertFalse(pages["items"].entries[0].action.enabled)

    def test_berserk_enables_only_attack(self) -> None:
        pages = self.runtime.build_pages(
            self._restriction_state(status_mask=runtime_catalog.BERSERK_STATUS_MASK),
            summon_active=True,
        )
        root = {entry.name: entry for entry in pages["root"].entries}

        self.assertTrue(root["Attack"].enabled)
        self.assertTrue(root["Attack"].action.enabled)
        for name, entry in root.items():
            if name != "Attack":
                self.assertFalse(entry.enabled, name)
        self.assertFalse(pages["technicks"].entries[0].action.enabled)
        self.assertFalse(pages["items"].entries[0].action.enabled)

    def test_berserk_does_not_override_a_harder_status_or_attack_seal(self) -> None:
        disabled = self.runtime.build_pages(
            self._restriction_state(
                status_mask=(runtime_catalog.BERSERK_STATUS_MASK | (1 << 13))
            )
        )
        sealed = self.runtime.build_pages(
            self._restriction_state(
                status_mask=runtime_catalog.BERSERK_STATUS_MASK,
                command_restrictions=runtime_catalog.ATTACK_COMMAND_RESTRICTION,
            )
        )

        self.assertTrue(all(not entry.enabled for entry in disabled["root"].entries))
        self.assertFalse(
            next(
                entry for entry in sealed["root"].entries if entry.name == "Attack"
            ).enabled
        )

    def test_pharos_seals_only_the_matching_command_family(self) -> None:
        expected = {
            runtime_catalog.ATTACK_COMMAND_RESTRICTION: "Attack",
            runtime_catalog.MAGICK_COMMAND_RESTRICTION: "White Magicks",
            runtime_catalog.TECHNICK_COMMAND_RESTRICTION: "Technicks",
            runtime_catalog.ITEM_COMMAND_RESTRICTION: "Items",
        }
        for seal, blocked_name in expected.items():
            with self.subTest(seal=seal):
                pages = self.runtime.build_pages(
                    self._restriction_state(command_restrictions=seal)
                )
                root = {entry.name: entry for entry in pages["root"].entries}
                categories = {entry.name: entry for entry in pages["magicks"].entries}
                if blocked_name in root:
                    self.assertFalse(root[blocked_name].enabled)
                else:
                    self.assertFalse(categories[blocked_name].enabled)

                if blocked_name != "Attack":
                    self.assertTrue(root["Attack"].enabled)
                if blocked_name != "Items":
                    self.assertTrue(root["Items"].enabled)

    def test_attack_seal_does_not_block_dismiss(self) -> None:
        pages = self.runtime.build_pages(
            self._restriction_state(
                command_restrictions=runtime_catalog.ATTACK_COMMAND_RESTRICTION
            ),
            summon_active=True,
        )
        root = {entry.name: entry for entry in pages["root"].entries}
        self.assertFalse(root["Attack"].enabled)
        self.assertTrue(root["Dismiss"].enabled)

    def test_live_status_is_part_of_the_catalog_generation(self) -> None:
        owner = 0x100000
        battle = 0x300000
        actor = 0x500000
        battle_keep = 0x600000

        class FakeProcess:
            def u64(self, address: int) -> int:
                return {
                    runtime_catalog.MAIN_SAVE_OWNER_GLOBAL: owner,
                    runtime_catalog.BATTLE_SAVE_BASE_GLOBAL: battle,
                    actor + 0x698: battle_keep,
                }.get(address, 0)

            def read(self, address: int, size: int) -> bytes:
                if address == runtime_catalog.COMMAND_RESTRICTIONS_GLOBAL:
                    return bytes((runtime_catalog.ITEM_COMMAND_RESTRICTION,))
                if (
                    address
                    == battle_keep + runtime_catalog.TEMPORARY_STATUS_EFFECTS_OFFSET
                ):
                    return struct.pack("<I", runtime_catalog.SILENCE_STATUS_MASK)
                return bytes(size)

        state = self.runtime.read_state(FakeProcess(), live_actor=actor)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.status_mask, runtime_catalog.SILENCE_STATUS_MASK)
        self.assertEqual(
            state.command_restrictions,
            runtime_catalog.ITEM_COMMAND_RESTRICTION,
        )
        self.assertTrue(state.signature.endswith(struct.pack("<I", state.status_mask)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
