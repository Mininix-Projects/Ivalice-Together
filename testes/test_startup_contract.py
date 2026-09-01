import struct
import time
import unittest
from unittest import mock

import start_headless_coop as launcher


BASE = 0x5BAA0000
DUAL_BASE = 0x61000000
DAMAGE_BASE = 0x62000000
BATTLE_INFO = 0x70000000
MENU_INFO = 0x74000000
P1_ACTOR = 0x71000000
P2_ACTOR = 0x72000000
P3_ACTOR = 0x73000000
EXPECTED_DEVICE_MASK = 0x0E


class FakeMemoryProcess:
    """Exact-address memory used by the final startup contract."""

    def __init__(self):
        self.memory = {}

    def put(self, address, data):
        self.memory[(int(address), len(data))] = bytes(data)

    def put_u32(self, address, value):
        self.put(address, struct.pack("<I", int(value)))

    def put_u64(self, address, value):
        self.put(address, struct.pack("<Q", int(value)))

    def read(self, address, size):
        try:
            return self.memory[(int(address), int(size))]
        except KeyError as error:
            raise OSError(
                f"unexpected fake-memory read at {address:#x} size={size}"
            ) from error

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]


def _theme_selection():
    return launcher.menu_themes.ThemeSelection(
        player_1="blue",
        player_2="white",
        player_3="gold",
        character_overrides=(
            (0, "blue"),
            (4, "emerald"),
            (5, "gold"),
        ),
        assignment_mode="character",
    )


def _palette(theme):
    return struct.pack(
        "<8i",
        *launcher.menu_themes.gauge_palette(theme),
    )


def _expected_patch(site, target, *, trailing_nops=0):
    return b"\xe9" + launcher.coop.rel32(site, 5, target) + b"\x90" * trailing_nops


def make_ready_process():
    process = FakeMemoryProcess()
    command_block = BASE + 0x200
    p3_block = BASE + launcher.coop.P3_COMMAND_BLOCK_OFFSET

    process.put(
        launcher.coop.MOVEMENT_HOOK_SITE,
        _expected_patch(
            launcher.coop.MOVEMENT_HOOK_SITE,
            BASE + launcher.coop.MOVEMENT_CAVE_OFFSET,
        ),
    )
    process.put(
        BASE + launcher.MOVEMENT_LIFETIME_READY_OFFSET,
        launcher.coop.MOVEMENT_HELPER_LIVENESS_STRUCT.pack(
            2,
            launcher.coop.MOVEMENT_HELPER_LIVENESS_MAGIC,
            launcher.coop.MOVEMENT_HELPER_LIVENESS_VERSION,
            4321,
            max(1, int(time.monotonic() * 1000.0)),
            0,
            0,
        ),
    )
    # The seqlock reader samples the leading sequence independently before and
    # after reading the complete record.
    process.put_u32(BASE + launcher.MOVEMENT_LIFETIME_READY_OFFSET, 2)
    process.put(
        launcher.coop.GAUGE_COLOR_HOOK_SITE,
        _expected_patch(
            launcher.coop.GAUGE_COLOR_HOOK_SITE,
            BASE + 0x900,
            trailing_nops=3,
        ),
    )
    process.put(
        launcher.coop.KEYBOARD_HOOK_SITE,
        _expected_patch(
            launcher.coop.KEYBOARD_HOOK_SITE,
            BASE + 0x300,
            trailing_nops=3,
        ),
    )
    process.put(
        launcher.coop.XINPUT_DEVICE_FINALIZE_SITE,
        _expected_patch(
            launcher.coop.XINPUT_DEVICE_FINALIZE_SITE,
            DUAL_BASE + launcher.coop.P2_DEVICE_FILTER_CAVE_OFFSET,
        ),
    )
    process.put(
        launcher.coop.XINPUT_STATE_RESULT_SITE,
        _expected_patch(
            launcher.coop.XINPUT_STATE_RESULT_SITE,
            DUAL_BASE + launcher.coop.P1_SDL_XINPUT_CAVE_OFFSET,
            trailing_nops=2,
        ),
    )
    process.put(
        launcher.coop.DINPUT_GAMEPAD_FINALIZE_SITE,
        _expected_patch(
            launcher.coop.DINPUT_GAMEPAD_FINALIZE_SITE,
            DUAL_BASE + launcher.coop.DINPUT_GAMEPAD_FILTER_CAVE_OFFSET,
            trailing_nops=3,
        ),
    )
    process.put(
        launcher.coop.STEAM_GAMEPAD_FINALIZE_SITE,
        _expected_patch(
            launcher.coop.STEAM_GAMEPAD_FINALIZE_SITE,
            DUAL_BASE + launcher.coop.STEAM_GAMEPAD_FILTER_CAVE_OFFSET,
            trailing_nops=3,
        ),
    )
    process.put(DUAL_BASE, launcher.coop.DUAL_INTERACTION_MAGIC)
    process.put_u32(
        BASE + 0x300 + launcher.coop.KEYBOARD_FILTER_BUDGET_CAVE_OFFSET,
        launcher.coop.KEYBOARD_FILTER_BUDGET_REFILL,
    )
    process.put_u32(
        DUAL_BASE + launcher.coop.P2_DEVICE_FILTER_STATE_OFFSET,
        EXPECTED_DEVICE_MASK,
    )
    process.put_u32(
        DUAL_BASE
        + launcher.coop.P2_DEVICE_FILTER_STATE_OFFSET
        + launcher.coop.P1_NATIVE_CONTROLLER_SLOT_OFFSET,
        0,
    )
    process.put(
        DAMAGE_BASE,
        launcher.coop.DAMAGE_NUMBER_THEME_MAGIC,
    )
    process.put(
        launcher.coop.DAMAGE_NUMBER_SOURCE_CALL_SITE,
        b"\xe8"
        + launcher.coop.rel32(
            launcher.coop.DAMAGE_NUMBER_SOURCE_CALL_SITE,
            5,
            DAMAGE_BASE + launcher.coop.DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET,
        ),
    )
    loot_workspace = DAMAGE_BASE + launcher.coop.LOOT_OWNERSHIP_WORKSPACE_OFFSET
    loot_code = loot_workspace + launcher.loot_ownership.CAVE_CODE_OFFSET
    process.put(loot_workspace, launcher.loot_ownership.CAVE_MAGIC)
    process.put(
        launcher.loot_ownership.OWNERSHIP_CALL_SITE,
        launcher.loot_ownership.call_site_patch(loot_code),
    )

    # Default/Menu own the complete unlocked roster. Current/UI contain only
    # the active field party, exactly as they do after using the Party menu.
    roster_layout = (0, 4, 5, 0xFFFF, 1, 2, 3, 0xFFFF, 0xFFFF)
    active_layout = (0, 4, 5, *([0xFFFF] * 6))
    process.put_u64(launcher.BATTLE_INFO_GLOBAL, BATTLE_INFO)
    for layout_id in (
        launcher.session_state.BATTLE_DEFAULT_LAYOUT,
        launcher.session_state.BATTLE_MENU_LAYOUT,
    ):
        process.put(
            BATTLE_INFO
            + launcher.session_state.BATTLE_PARTY_LAYOUT_OFFSET
            + layout_id * launcher.session_state.BATTLE_PARTY_LAYOUT_STRIDE,
            struct.pack("<9H", *roster_layout),
        )
    for layout_id in (
        launcher.session_state.BATTLE_CURRENT_LAYOUT,
        launcher.session_state.BATTLE_UI_LAYOUT,
    ):
        process.put(
            BATTLE_INFO
            + launcher.session_state.BATTLE_PARTY_LAYOUT_OFFSET
            + layout_id * launcher.session_state.BATTLE_PARTY_LAYOUT_STRIDE,
            struct.pack("<9H", *active_layout),
        )
    process.put_u32(
        BATTLE_INFO + launcher.session_state.BATTLE_LEADER_OFFSET,
        0,
    )

    process.put_u64(command_block + 0x10, P2_ACTOR)
    process.put_u32(
        command_block + launcher.coop.P1_HUD_PARTY_SLOT_OFFSET,
        0,
    )
    process.put_u32(
        command_block + launcher.coop.P2_HUD_PARTY_SLOT_OFFSET,
        1,
    )
    process.put_u32(
        command_block + launcher.coop.COOP_ACTIVE_OFFSET,
        1,
    )
    process.put_u32(
        command_block + launcher.coop.MOVEMENT_ACTIVE_OFFSET,
        1,
    )
    process.put_u64(p3_block + launcher.coop.P3_ACTOR_OFFSET, P3_ACTOR)
    process.put_u32(
        p3_block + launcher.coop.P3_MOVEMENT_ACTIVE_OFFSET,
        1,
    )
    process.put_u32(
        p3_block + launcher.coop.P3_HUD_PARTY_SLOT_OFFSET,
        2,
    )

    process.put(BASE + launcher.P1_GAUGE_THEME_PALETTE_OFFSET, _palette("blue"))
    process.put(
        BASE + launcher.P2_GAUGE_THEME_PALETTE_OFFSET,
        _palette("emerald"),
    )
    process.put(BASE + launcher.P3_GAUGE_THEME_PALETTE_OFFSET, _palette("gold"))

    process.put_u64(launcher.story_party_patch.MENU_INFO_GLOBAL, MENU_INFO)
    menu_cave = BASE + launcher.story_party_patch.MENU_FEATURE_GATE_CAVE_OFFSET
    menu_state = BASE + launcher.story_party_patch.MENU_FEATURE_GATE_STATE_OFFSET
    process.put(
        launcher.story_party_patch.MENU_FEATURE_GATE_SITE,
        _expected_patch(
            launcher.story_party_patch.MENU_FEATURE_GATE_SITE,
            menu_cave,
        ),
    )
    process.put(
        menu_cave,
        launcher.story_party_patch.build_early_menu_feature_gate(
            menu_cave,
            menu_state,
        ),
    )
    process.put(
        MENU_INFO + launcher.story_party_patch.MENU_SELECTABLE_FLAGS_OFFSET,
        bytes((launcher.story_party_patch.MENU_START_SELECTABLE_MASK,)),
    )
    process.put(
        BASE + launcher.story_party_patch.MENU_FEATURE_GATE_STATE_OFFSET,
        b"\x01",
    )
    process.put_u32(BASE + launcher.RING_READY_OFFSET, launcher.RING_READY_MAGIC)
    return process


class StartupMemoryContractTests(unittest.TestCase):
    identity = launcher.session_state.SessionIdentity(0, 4, 5)
    actor_slots = {
        0: (10, P1_ACTOR, 0x71100000, (0.0, 0.0, 0.0)),
        1: (11, P2_ACTOR, 0x72100000, (1.0, 0.0, 0.0)),
        2: (12, P3_ACTOR, 0x73100000, (2.0, 0.0, 0.0)),
    }

    def snapshot(self, process, *, start_with_all=True, identity=None):
        with (
            mock.patch.object(
                launcher.coop,
                "COMMAND_BLOCK",
                BASE + 0x200,
            ),
            mock.patch.object(
                launcher.coop,
                "resolve_active_actors",
                return_value=list(self.actor_slots.values()),
            ),
            mock.patch.object(
                launcher.coop,
                "resolve_active_party_actor_slots",
                return_value=self.actor_slots,
            ),
        ):
            return launcher.runtime_readiness_snapshot(
                process,
                BASE,
                DUAL_BASE,
                DAMAGE_BASE,
                identity or self.identity,
                start_with_all=start_with_all,
                theme_selection=_theme_selection(),
                expected_device_mask=EXPECTED_DEVICE_MASK,
            )

    def classified_snapshot(self, process, *, start_with_all=True, identity=None):
        with (
            mock.patch.object(
                launcher.coop,
                "COMMAND_BLOCK",
                BASE + 0x200,
            ),
            mock.patch.object(
                launcher.coop,
                "resolve_active_actors",
                return_value=list(self.actor_slots.values()),
            ),
            mock.patch.object(
                launcher.coop,
                "resolve_active_party_actor_slots",
                return_value=self.actor_slots,
            ),
        ):
            return launcher.classified_runtime_readiness_snapshot(
                process,
                BASE,
                DUAL_BASE,
                DAMAGE_BASE,
                identity or self.identity,
                start_with_all=start_with_all,
                theme_selection=_theme_selection(),
                expected_device_mask=EXPECTED_DEVICE_MASK,
            )

    def test_complete_live_generation_is_ready(self):
        ready, diagnostic = self.snapshot(make_ready_process())
        self.assertTrue(ready, diagnostic)

    def test_story_pair_can_commit_while_a_configured_third_player_is_latent(self):
        process = make_ready_process()
        live_pair = (0, 5, *([0xFFFF] * 7))
        for layout_id in (
            launcher.session_state.BATTLE_CURRENT_LAYOUT,
            launcher.session_state.BATTLE_UI_LAYOUT,
        ):
            process.put(
                BATTLE_INFO
                + launcher.session_state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * launcher.session_state.BATTLE_PARTY_LAYOUT_STRIDE,
                struct.pack("<9H", *live_pair),
            )
        process.put(
            BASE + launcher.P2_GAUGE_THEME_PALETTE_OFFSET,
            _palette("gold"),
        )

        ready, diagnostic = self.snapshot(
            process,
            start_with_all=False,
            identity=launcher.session_state.SessionIdentity(1, 4, 3),
        )

        self.assertTrue(ready, diagnostic)
        self.assertIn("roles={'p1': 0, 'p2': 5}", diagnostic)

    def test_solo_p1_can_commit_with_configured_followers_in_reserve(self):
        process = make_ready_process()
        solo = (5, *([0xFFFF] * 8))
        for layout_id in (
            launcher.session_state.BATTLE_CURRENT_LAYOUT,
            launcher.session_state.BATTLE_UI_LAYOUT,
        ):
            process.put(
                BATTLE_INFO
                + launcher.session_state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * launcher.session_state.BATTLE_PARTY_LAYOUT_STRIDE,
                struct.pack("<9H", *solo),
            )
        process.put_u32(
            BATTLE_INFO + launcher.session_state.BATTLE_LEADER_OFFSET,
            5,
        )
        process.put(BASE + launcher.P1_GAUGE_THEME_PALETTE_OFFSET, _palette("gold"))

        ready, diagnostic = self.snapshot(
            process,
            identity=launcher.session_state.SessionIdentity(5, 4, 3),
        )

        self.assertTrue(ready, diagnostic)
        self.assertIn("roles={'p1': 5}", diagnostic)
        self.assertIn("slots={'p1': 0}", diagnostic)

    def test_damage_theme_and_loot_hooks_are_part_of_final_commit(self):
        mutations = (
            (
                launcher.coop.DAMAGE_NUMBER_SOURCE_CALL_SITE,
                launcher.coop.DAMAGE_NUMBER_SOURCE_CALL_ORIGINAL,
                "damage=0",
            ),
            (
                launcher.loot_ownership.OWNERSHIP_CALL_SITE,
                launcher.loot_ownership.STOCK_CALL,
                "loot=0",
            ),
        )
        for address, replacement, diagnostic_fragment in mutations:
            with self.subTest(address=f"{address:#x}"):
                process = make_ready_process()
                process.put(address, replacement)
                ready, diagnostic = self.snapshot(process)
                self.assertFalse(ready, diagnostic)
                self.assertIn(diagnostic_fragment, diagnostic)

    def test_incomplete_permanent_roster_is_degraded_not_structurally_fatal(self):
        process = make_ready_process()
        incomplete = (
            0,
            4,
            5,
            0xFFFF,
            0xFFFF,
            2,
            3,
            0xFFFF,
            0xFFFF,
        )
        process.put(
            BATTLE_INFO
            + launcher.session_state.BATTLE_PARTY_LAYOUT_OFFSET
            + launcher.session_state.BATTLE_DEFAULT_LAYOUT
            * launcher.session_state.BATTLE_PARTY_LAYOUT_STRIDE,
            struct.pack("<9H", *incomplete),
        )

        snapshot = self.classified_snapshot(process, start_with_all=True)
        self.assertTrue(snapshot.structural_ready, snapshot.diagnostic)
        self.assertFalse(snapshot.operational_ready, snapshot.diagnostic)
        self.assertEqual(snapshot.operational_state, "degraded")
        self.assertIn("roster=0", snapshot.diagnostic)

    def test_start_all_accepts_active_trio_without_vaan(self):
        process = make_ready_process()
        active = (2, 1, 5, *([0xFFFF] * 6))
        for layout_id in (
            launcher.session_state.BATTLE_CURRENT_LAYOUT,
            launcher.session_state.BATTLE_UI_LAYOUT,
        ):
            process.put(
                BATTLE_INFO
                + launcher.session_state.BATTLE_PARTY_LAYOUT_OFFSET
                + layout_id * launcher.session_state.BATTLE_PARTY_LAYOUT_STRIDE,
                struct.pack("<9H", *active),
            )
        process.put_u32(
            BATTLE_INFO + launcher.session_state.BATTLE_LEADER_OFFSET,
            2,
        )
        process.put(
            BASE + launcher.P1_GAUGE_THEME_PALETTE_OFFSET,
            _palette("purple"),
        )
        process.put(
            BASE + launcher.P2_GAUGE_THEME_PALETTE_OFFSET,
            _palette("pink"),
        )

        identity = launcher.session_state.SessionIdentity(2, 1, 5)
        ready, diagnostic = self.snapshot(process, identity=identity)
        self.assertTrue(ready, diagnostic)

    def test_theme_transition_is_degraded_not_structurally_fatal(self):
        process = make_ready_process()
        snapshot = self.classified_snapshot(process)
        self.assertTrue(snapshot.structural_ready, snapshot.diagnostic)
        self.assertTrue(snapshot.operational_ready, snapshot.diagnostic)

        process.put(
            BASE + launcher.P2_GAUGE_THEME_PALETTE_OFFSET,
            _palette("gold"),
        )
        snapshot = self.classified_snapshot(process)
        self.assertTrue(snapshot.structural_ready, snapshot.diagnostic)
        self.assertFalse(snapshot.operational_ready, snapshot.diagnostic)
        self.assertIn("themes=0", snapshot.diagnostic)

        process.put(
            BASE + launcher.P2_GAUGE_THEME_PALETTE_OFFSET,
            _palette("emerald"),
        )
        snapshot = self.classified_snapshot(process)
        self.assertTrue(snapshot.structural_ready, snapshot.diagnostic)
        self.assertTrue(snapshot.operational_ready, snapshot.diagnostic)

    def test_missing_input_hook_is_structurally_rejected(self):
        mutations = (
            (launcher.coop.GAUGE_COLOR_HOOK_SITE, b"\x90" * 8),
            (launcher.coop.KEYBOARD_HOOK_SITE, b"\x90" * 8),
            (launcher.coop.XINPUT_DEVICE_FINALIZE_SITE, b"\x90" * 5),
            (launcher.coop.XINPUT_STATE_RESULT_SITE, b"\x90" * 7),
            (launcher.coop.DINPUT_GAMEPAD_FINALIZE_SITE, b"\x90" * 8),
            (launcher.coop.STEAM_GAMEPAD_FINALIZE_SITE, b"\x90" * 8),
        )
        for address, replacement in mutations:
            with self.subTest(address=f"{address:#x}"):
                process = make_ready_process()
                process.put(address, replacement)
                ready, diagnostic = self.snapshot(process)
                self.assertFalse(ready, diagnostic)

    def test_input_policy_transitions_do_not_fail_structural_readiness(self):
        mutations = (
            (
                DUAL_BASE + launcher.coop.P2_DEVICE_FILTER_STATE_OFFSET,
                struct.pack("<I", 0),
                True,
            ),
            (
                BASE + 0x300 + launcher.coop.KEYBOARD_FILTER_BUDGET_CAVE_OFFSET,
                struct.pack("<I", 0),
                False,
            ),
        )
        for address, replacement, operational_ready in mutations:
            with self.subTest(address=f"{address:#x}"):
                process = make_ready_process()
                process.put(address, replacement)
                snapshot = self.classified_snapshot(process)
                self.assertTrue(snapshot.structural_ready, snapshot.diagnostic)
                self.assertEqual(
                    snapshot.operational_ready,
                    operational_ready,
                    snapshot.diagnostic,
                )

    def test_selectable_menu_bits_without_the_live_feature_gate_are_rejected(self):
        process = make_ready_process()
        process.put(
            launcher.story_party_patch.MENU_FEATURE_GATE_SITE,
            launcher.story_party_patch.MENU_FEATURE_GATE_ORIGINAL,
        )

        ready, diagnostic = self.snapshot(process)
        self.assertFalse(ready, diagnostic)
        self.assertIn("menu_hook=0", diagnostic)

    def test_p3_actor_active_bit_and_hud_slot_transitions_are_nonfatal(self):
        p3_block = BASE + launcher.coop.P3_COMMAND_BLOCK_OFFSET
        mutations = (
            (
                p3_block + launcher.coop.P3_ACTOR_OFFSET,
                struct.pack("<Q", 0),
            ),
            (
                p3_block + launcher.coop.P3_MOVEMENT_ACTIVE_OFFSET,
                struct.pack("<I", 0),
            ),
            (
                p3_block + launcher.coop.P3_HUD_PARTY_SLOT_OFFSET,
                struct.pack("<I", launcher.INVALID_HUD_PARTY_SLOT),
            ),
        )
        for address, replacement in mutations:
            with self.subTest(address=f"{address:#x}"):
                process = make_ready_process()
                process.put(address, replacement)
                snapshot = self.classified_snapshot(process)
                self.assertTrue(snapshot.structural_ready, snapshot.diagnostic)
                self.assertFalse(snapshot.operational_ready, snapshot.diagnostic)

    def test_ko_stone_loading_and_manual_toggle_observations_are_nonfatal(self):
        command_block = BASE + 0x200
        mutations = (
            (
                "ko-or-rematerialization-p2-actor",
                command_block + 0x10,
                struct.pack("<Q", 0),
            ),
            (
                "stone-or-action-gate-p2-movement",
                command_block + launcher.coop.MOVEMENT_ACTIVE_OFFSET,
                struct.pack("<I", 0),
            ),
            (
                "f8-coop-toggle",
                command_block + launcher.coop.COOP_ACTIVE_OFFSET,
                struct.pack("<I", 0),
            ),
            (
                "loading-hud-unmaterialized",
                command_block + launcher.coop.P1_HUD_PARTY_SLOT_OFFSET,
                struct.pack("<I", launcher.INVALID_HUD_PARTY_SLOT),
            ),
        )
        for label, address, replacement in mutations:
            with self.subTest(label=label):
                process = make_ready_process()
                process.put(address, replacement)
                snapshot = self.classified_snapshot(process)
                self.assertTrue(snapshot.structural_ready, snapshot.diagnostic)
                self.assertFalse(snapshot.operational_ready, snapshot.diagnostic)

    def test_incoherent_party_generation_is_a_transition_not_a_failure(self):
        process = make_ready_process()
        loading_current = (0, 0, 4, 5, *([0xFFFF] * 5))
        process.put(
            BATTLE_INFO
            + launcher.session_state.BATTLE_PARTY_LAYOUT_OFFSET
            + launcher.session_state.BATTLE_CURRENT_LAYOUT
            * launcher.session_state.BATTLE_PARTY_LAYOUT_STRIDE,
            struct.pack("<9H", *loading_current),
        )

        snapshot = self.classified_snapshot(process)

        self.assertTrue(snapshot.structural_ready, snapshot.diagnostic)
        self.assertFalse(snapshot.operational_ready, snapshot.diagnostic)
        self.assertEqual(snapshot.operational_state, "transition")


if __name__ == "__main__":
    unittest.main()
