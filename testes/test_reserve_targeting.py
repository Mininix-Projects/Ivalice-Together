"""Native-compatible targeting of party members outside the active formation."""

from __future__ import annotations

import struct
import unittest

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

from battle_pack_runtime_catalog import BattlePackGameplayCatalog
from headless_menu_core import (
    SUBMIT_DISMISS_ACTION,
    SUBMIT_MANUAL_ACTION,
    SUBMIT_RESERVE_ACTION,
    build_dispatcher,
)
from headless_menu_model import (
    ActionDefinition,
    EntryKind,
    MenuEntry,
    P2MenuModel,
    PageDefinition,
    TargetCandidate,
    TargetGroup,
)
from headless_menu_shell import complete_party_layout, reserve_party_candidates
import session_state


ISSUER = 0x100009
FOE = 0x200010
ACTIVE = 0x100008
RESERVE_MEMBER_ID = 5


def reserve_model(*, with_foe: bool) -> P2MenuModel:
    action = ActionDefinition(
        0x52,
        "Potion",
        "Restore HP to one ally.",
        (
            TargetGroup.PARTY,
            TargetGroup.NEUTRALS,
            TargetGroup.FOES,
            TargetGroup.RESERVE,
        ),
        TargetGroup.PARTY,
        "Items",
    )
    model = P2MenuModel(
        ISSUER,
        {
            "root": PageDefinition(
                "root",
                "",
                (MenuEntry("Potion", "", EntryKind.ACTION, action=action),),
            )
        },
    )
    candidates = [
        TargetCandidate(ACTIVE, "Vaan", TargetGroup.PARTY),
        TargetCandidate(
            0,
            "Penelo",
            TargetGroup.RESERVE,
            party_member_id=RESERVE_MEMBER_ID,
        ),
    ]
    if with_foe:
        candidates.append(TargetCandidate(FOE, "Wolf", TargetGroup.FOES))
    model.set_candidates(candidates)
    model.open()
    model.confirm()
    return model


class ReserveActionFlagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = BattlePackGameplayCatalog()

    def test_exact_native_reserve_action_set(self):
        eligible = {
            rules.action_id
            for rules in self.catalog.action_rules
            if rules.possible_reserve_party
        }
        self.assertEqual(
            eligible,
            {
                0x00,
                0x04,
                0x05,
                0x06,
                0x0B,
                0x0E,
                0x0F,
                0x52,
                0x53,
                0x54,
                0x57,
                0x58,
                0x67,
                0x86,
            },
        )

    def test_revive_technick_does_not_gain_reserve_targeting(self):
        self.assertFalse(self.catalog.rules(0xAE).possible_reserve_party)
        self.assertNotIn(
            TargetGroup.RESERVE,
            self.catalog.rules(0xAE).target_groups,
        )


class ReserveCycleTests(unittest.TestCase):
    def test_without_foes_one_press_reaches_reserve(self):
        model = reserve_model(with_foe=False)
        self.assertEqual(model.target_group, TargetGroup.PARTY)
        self.assertEqual(model.cycle_target_group().value, TargetGroup.RESERVE)

    def test_with_foes_two_presses_reach_reserve(self):
        model = reserve_model(with_foe=True)
        self.assertEqual(model.cycle_target_group().value, TargetGroup.FOES)
        self.assertEqual(model.cycle_target_group().value, TargetGroup.RESERVE)

    def test_submit_carries_member_id_and_no_world_handle(self):
        model = reserve_model(with_foe=False)
        model.cycle_target_group()
        event = model.confirm()
        self.assertEqual(event.kind, "submit")
        self.assertEqual(event.request.target, 0)
        self.assertEqual(event.request.reserve_party_member_id, RESERVE_MEMBER_ID)


class ReserveLayoutTests(unittest.TestCase):
    class LayoutProcess:
        def __init__(self, battle_info, default, menu):
            self.battle_info = battle_info
            self.layouts = {0: default, 3: menu}

        def read(self, address, size):
            layout_id = (
                address - self.battle_info - session_state.BATTLE_PARTY_LAYOUT_OFFSET
            ) // session_state.BATTLE_PARTY_LAYOUT_STRIDE
            return struct.pack("<9H", *self.layouts[layout_id])[:size]

    def test_only_non_materialized_reserve_slots_become_candidates(self):
        empty = session_state.EMPTY_PARTY_ID
        candidates = reserve_party_candidates(
            (0, 3, 2, empty, 1, 5, 3, empty, 5),
            {0, 2, 3},
        )
        self.assertEqual(
            [
                (candidate.name, candidate.handle, candidate.party_member_id)
                for candidate in candidates
            ],
            [("Ashe", 0, 1), ("Penelo", 0, 5)],
        )

    def test_complete_roster_comes_from_native_default_and_menu_layouts(self):
        empty = session_state.EMPTY_PARTY_ID
        battle_info = 0x50000000
        active = (4, 5, empty, empty, empty, empty, empty, empty, empty)
        complete = (4, 5, empty, empty, 0, 3, 2, 1, empty)
        process = self.LayoutProcess(battle_info, complete, complete)

        roster = complete_party_layout(process, battle_info, active)
        candidates = reserve_party_candidates(roster, {4, 5})

        self.assertEqual(roster, complete)
        self.assertEqual(
            [candidate.party_member_id for candidate in candidates],
            [0, 3, 2, 1],
        )

    def test_temporary_esper_does_not_erase_reserve_targets(self):
        empty = session_state.EMPTY_PARTY_ID
        battle_info = 0x50000000
        complete = (3, 1, 5, empty, 0, 2, 4, empty, empty)
        current = (3, 1, 5, 33, empty, empty, empty, empty, empty)
        process = self.LayoutProcess(battle_info, complete, complete)

        roster = complete_party_layout(process, battle_info, current)
        candidates = reserve_party_candidates(roster, {3, 1, 5, 33})

        self.assertEqual(roster, complete)
        self.assertEqual(
            [candidate.party_member_id for candidate in candidates],
            [0, 2, 4],
        )

    def test_disagreeing_complete_layouts_fail_closed_to_current(self):
        empty = session_state.EMPTY_PARTY_ID
        battle_info = 0x50000000
        active = (4, 5, empty, empty, empty, empty, empty, empty, empty)
        default = (4, 5, empty, empty, 0, 3, 2, 1, empty)
        menu = (4, 5, empty, empty, 0, 3, 2, empty, empty)
        process = self.LayoutProcess(battle_info, default, menu)

        self.assertEqual(
            complete_party_layout(process, battle_info, active),
            active,
        )


class ReserveDispatcherTests(unittest.TestCase):
    @staticmethod
    def disassembly(code: bytes, address: int) -> str:
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        return " | ".join(
            f"{instruction.mnemonic} {instruction.op_str}"
            for instruction in decoder.disasm(code, address)
        )

    def test_reserve_route_matches_the_native_type_0f_call_contract(self):
        cave = 0x6100E000
        code = build_dispatcher(
            cave,
            0x6100E800,
            ISSUER,
            0,
            0x52,
            RESERVE_MEMBER_ID,
        )
        text = self.disassembly(code, cave)
        self.assertIn("xor edx, edx", text)
        self.assertIn("mov r8d, 5", text)
        self.assertIn("mov r9d, 0x52", text)
        self.assertIn("mov dword ptr [rsp + 0x20], 0", text)
        self.assertIn(f"movabs rax, {SUBMIT_RESERVE_ACTION:#x}", text)

    def test_normal_route_still_uses_the_existing_manual_entrypoint(self):
        cave = 0x6100E000
        text = self.disassembly(
            build_dispatcher(cave, 0x6100E800, ISSUER, FOE, 0x96),
            cave,
        )
        self.assertIn("mov r8d, 0x96", text)
        self.assertIn(f"movabs rax, {SUBMIT_MANUAL_ACTION:#x}", text)

    def test_dismiss_uses_the_native_summon_shutdown_helper(self):
        cave = 0x6100E000
        text = self.disassembly(
            build_dispatcher(cave, 0x6100E800, ISSUER, ISSUER, 0x113),
            cave,
        )

        self.assertIn("xor ecx, ecx", text)
        self.assertIn(f"movabs rax, {SUBMIT_DISMISS_ACTION:#x}", text)
        self.assertNotIn(f"movabs rax, {SUBMIT_MANUAL_ACTION:#x}", text)
        self.assertNotIn(f"movabs rax, {SUBMIT_RESERVE_ACTION:#x}", text)

    def test_member_id_must_fit_the_native_byte_field(self):
        with self.assertRaisesRegex(ValueError, "one byte"):
            build_dispatcher(
                0x6100E000,
                0x6100E800,
                ISSUER,
                0,
                0x52,
                0x100,
            )


if __name__ == "__main__":
    unittest.main()
