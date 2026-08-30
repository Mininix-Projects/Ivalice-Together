from __future__ import annotations

import unittest
import struct
from dataclasses import fields
from types import SimpleNamespace

import headless_menu_shell as live
from battle_pack_runtime_catalog import ActionRules


class FakeProcess:
    def __init__(self, shift: int) -> None:
        self.dwords = {live.COMBAT_LOG_MENU_SHIFT_GLOBAL: shift}
        self.writes: list[tuple[int, int]] = []

    def u32(self, address: int) -> int:
        return self.dwords.get(address, 0)

    def write(self, address: int, data: bytes) -> None:
        value = struct.unpack("<I", data)[0]
        self.dwords[address] = value
        self.writes.append((address, value))


class FakeVisibilityProcess:
    def __init__(self, alpha: int) -> None:
        self.manager = 0x10000000
        self.root = 0x20000000
        offset, callback, alpha_offset = live.COMBAT_LOG_ROOTS[0]
        self.alpha_address = self.root + alpha_offset
        self.qwords = {
            live.UI_MANAGER_GLOBAL: self.manager,
            self.manager + offset: self.root,
            self.root: callback,
        }
        self.alpha = alpha

    def u64(self, address: int) -> int:
        return self.qwords.get(address, 0)

    def read(self, address: int, size: int) -> bytes:
        if address != self.alpha_address or size != 2:
            raise OSError("unknown fake visibility address")
        return struct.pack("<H", self.alpha)


class FakePatchProcess:
    def __init__(self) -> None:
        self.bytes = {
            0x28EBDF: bytes.fromhex("E8 8C A0 16 00"),
            0x28EDAD: bytes.fromhex("E8 FE 74 03 00"),
            live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_SITE: live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_ORIGINAL,
            **{
                address: live.NATIVE_COMBAT_LOG_DRAW_ORIGINAL
                for address in live.NATIVE_COMBAT_LOG_DRAW_SITES
            },
        }
        self.patches: list[tuple[int, bytes]] = []

    def read(self, address: int, size: int) -> bytes:
        value = self.bytes[address]
        if len(value) != size:
            raise AssertionError("unexpected patch size")
        return value

    def patch_code(self, address: int, data: bytes) -> None:
        self.bytes[address] = bytes(data)
        self.patches.append((address, bytes(data)))


class FakeGateProcess:
    def __init__(self) -> None:
        self.memory: dict[int, int] = {}
        self.patches: list[tuple[int, bytes]] = []
        self.seed(
            live.NATIVE_COMBAT_LOG_TICK_CALL_SITE,
            live.NATIVE_COMBAT_LOG_TICK_CALL_ORIGINAL,
        )
        self.seed(
            live.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE,
            live.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_ORIGINAL,
        )
        for site, original, _cave_offset in live.NATIVE_COMBAT_LOG_LAYOUT_GATE_SITES:
            self.seed(site, original)

    def seed(self, address: int, data: bytes) -> None:
        for index, value in enumerate(data):
            self.memory[address + index] = value

    def read(self, address: int, size: int) -> bytes:
        return bytes(self.memory.get(address + index, 0) for index in range(size))

    def write(self, address: int, data: bytes) -> None:
        self.seed(address, bytes(data))

    def patch_code(self, address: int, data: bytes) -> None:
        data = bytes(data)
        self.seed(address, data)
        self.patches.append((address, data))


class NativeCombatLogLayoutTests(unittest.TestCase):
    def test_stock_combat_log_render_paths_are_reversible(self):
        process = FakePatchProcess()
        visibility = live.NativeStockFieldHudVisibility(process)

        visibility.maintain(hide_combat_log=True)
        self.assertEqual(process.bytes[0x28EBDF], bytes.fromhex("E8 8C A0 16 00"))
        self.assertEqual(process.bytes[0x28EDAD], bytes.fromhex("E8 FE 74 03 00"))
        for address in live.NATIVE_COMBAT_LOG_DRAW_SITES:
            self.assertEqual(
                process.bytes[address],
                live.NATIVE_COMBAT_LOG_DRAW_HIDDEN,
            )
        self.assertEqual(
            process.bytes[live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_SITE],
            live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_HIDDEN,
        )
        self.assertTrue(visibility.combat_log_hidden)

        visibility.restore()
        self.assertFalse(visibility.combat_log_hidden)
        for address in live.NATIVE_COMBAT_LOG_DRAW_SITES:
            self.assertEqual(
                process.bytes[address],
                live.NATIVE_COMBAT_LOG_DRAW_ORIGINAL,
            )
        self.assertEqual(
            process.bytes[live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_SITE],
            live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_ORIGINAL,
        )
        self.assertEqual(process.bytes[0x28EBDF], bytes.fromhex("E8 8C A0 16 00"))
        self.assertEqual(process.bytes[0x28EDAD], bytes.fromhex("E8 FE 74 03 00"))

    def test_unknown_combat_log_text_patch_is_not_overwritten(self):
        process = FakePatchProcess()
        process.bytes[live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_SITE] = b"\xcc" * 5
        visibility = live.NativeStockFieldHudVisibility(process)

        visibility.maintain(hide_combat_log=True)

        self.assertEqual(
            process.bytes[live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_SITE],
            b"\xcc" * 5,
        )
        self.assertFalse(visibility.combat_log_hidden)
        self.assertFalse(
            visibility.compatible[live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_SITE]
        )

    def test_exact_resident_combat_log_suppression_is_adopted_read_only(self):
        process = FakePatchProcess()
        for address in live.NATIVE_COMBAT_LOG_DRAW_SITES:
            process.bytes[address] = live.NATIVE_COMBAT_LOG_DRAW_HIDDEN
        process.bytes[live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_SITE] = (
            live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_HIDDEN
        )
        visibility = live.NativeStockFieldHudVisibility(process)

        visibility.maintain(hide_combat_log=True)
        visibility.restore()

        self.assertEqual(process.patches, [])
        for address in live.NATIVE_COMBAT_LOG_DRAW_SITES:
            self.assertEqual(
                process.bytes[address],
                live.NATIVE_COMBAT_LOG_DRAW_HIDDEN,
            )
        self.assertEqual(
            process.bytes[live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_SITE],
            live.NATIVE_COMBAT_LOG_TEXT_DRAW_CALL_HIDDEN,
        )

    def test_partial_combat_log_suppression_is_never_hot_repaired(self):
        process = FakePatchProcess()
        process.bytes[live.NATIVE_COMBAT_LOG_DRAW_SITES[0]] = (
            live.NATIVE_COMBAT_LOG_DRAW_HIDDEN
        )
        before = dict(process.bytes)
        visibility = live.NativeStockFieldHudVisibility(process)

        visibility.maintain(hide_combat_log=True)

        self.assertEqual(process.patches, [])
        self.assertEqual(process.bytes, before)
        self.assertFalse(visibility.combat_log_hidden)

    def test_target_info_gate_hooks_only_ui_thread_lifecycle_edges(self):
        process = FakeGateProcess()
        base = 0x50000000
        gate = live.NativeCombatLogTargetInfoGate(process, base)

        gate.install()

        self.assertTrue(gate.owns_static)

        self.assertEqual(
            process.read(live.NATIVE_COMBAT_LOG_TICK_CALL_SITE, 5),
            gate._tick_patch(),
        )
        self.assertEqual(
            process.read(live.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE, 5),
            gate._show_patch(),
        )
        tick_cave = process.read(gate.cave, len(gate._build_tick_cave()))
        show_cave = process.read(gate.show_cave, len(gate._build_show_cave()))
        for target in (
            live.NATIVE_COMBAT_LOG_TARGET_HIDE,
            live.NATIVE_COMBAT_LOG_TARGET_SHOW,
            live.NATIVE_COMBAT_LOG_TICK,
        ):
            self.assertIn(struct.pack("<Q", target), tick_cave)
        self.assertIn(
            struct.pack("<Q", live.NATIVE_COMBAT_LOG_TARGET_SHOW_BODY),
            show_cave,
        )
        self.assertEqual(
            process.read(gate.state + gate.MAGIC_OFFSET, 4),
            live.NATIVE_COMBAT_LOG_GATE_MAGIC,
        )
        self.assertIn(
            b"\x48\x39\x98"
            + struct.pack("<I", live.NATIVE_COMBAT_LOG_WIDGET_OWNER_OFFSET),
            tick_cave,
        )
        self.assertIn(
            b"\x48\x83\xb8"
            + struct.pack("<I", live.NATIVE_TARGET_INFO_OBJECT_OWNER_OFFSET)
            + b"\x00",
            tick_cave,
        )
        self.assertIn(
            b"\xf6\x80"
            + struct.pack("<I", live.NATIVE_TARGET_INFO_LIFECYCLE_FLAGS_OFFSET)
            + bytes((live.NATIVE_TARGET_INFO_LIFECYCLE_PENDING_MASK,)),
            tick_cave,
        )

        self.assertIn(
            gate._build_layout_gate_cave(owner_is_rdi=False),
            tick_cave,
        )
        self.assertIn(
            gate._build_layout_gate_cave(owner_is_rdi=True),
            tick_cave,
        )

    def test_target_info_gate_mailbox_is_edge_driven_and_reversible(self):
        process = FakeGateProcess()
        gate = live.NativeCombatLogTargetInfoGate(process, 0x50000000)
        gate.install()
        code_patch_count = len(process.patches)

        self.assertTrue(gate.request_hidden(True))
        self.assertEqual(process.read(gate.state, 1), b"\x01")
        self.assertEqual(len(process.patches), code_patch_count)
        process.write(gate.state + gate.APPLIED_OFFSET, b"\x01")
        self.assertTrue(gate.applied_hidden)

        self.assertTrue(gate.request_hidden(False))
        process.write(gate.state + gate.APPLIED_OFFSET, b"\x00")
        self.assertTrue(gate.restore(timeout=0.02))
        self.assertEqual(
            process.read(live.NATIVE_COMBAT_LOG_TICK_CALL_SITE, 5),
            gate._tick_patch(),
        )
        self.assertEqual(
            process.read(live.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE, 5),
            gate._show_patch(),
        )
        for site, patch in gate._layout_patches():
            self.assertEqual(process.read(site, len(patch)), patch)

    def test_target_info_gate_refuses_an_unknown_hook(self):
        process = FakeGateProcess()
        process.seed(live.NATIVE_COMBAT_LOG_TICK_CALL_SITE, b"\xcc" * 5)
        gate = live.NativeCombatLogTargetInfoGate(process, 0x50000000)

        with self.assertRaisesRegex(RuntimeError, "occupied/incomplete"):
            gate.install()

        self.assertEqual(
            process.read(live.NATIVE_COMBAT_LOG_TICK_CALL_SITE, 5),
            b"\xcc" * 5,
        )

    def test_target_info_gate_adopts_an_orphan_without_losing_applied_state(self):
        process = FakeGateProcess()
        first = live.NativeCombatLogTargetInfoGate(process, 0x50000000)
        first.install()
        first.request_hidden(True)
        process.write(first.state + first.APPLIED_OFFSET, b"\x01")

        adopted = live.NativeCombatLogTargetInfoGate(process, 0x50000000)
        adopted.install()

        self.assertFalse(adopted.owns_static)
        self.assertTrue(adopted.requested_hidden)
        self.assertEqual(process.read(adopted.state, 2), b"\x01\x01")
        process.write(adopted.state + adopted.APPLIED_OFFSET, b"\x00")
        self.assertTrue(adopted.restore(timeout=0.02))
        self.assertEqual(
            process.read(live.NATIVE_COMBAT_LOG_TICK_CALL_SITE, 5),
            adopted._tick_patch(),
        )
        self.assertEqual(
            process.read(live.NATIVE_COMBAT_LOG_TARGET_SHOW_TAIL_SITE, 5),
            adopted._show_patch(),
        )

    def test_custom_target_gate_tracks_real_cards_and_exit_fades(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        state_names = (
            "p1_target_info_handle",
            "p1_target_info_exit_started",
            "p2_target_info_presented",
            "target_visual_exit_started",
            "p3_target_info_handle",
            "p3_target_info_exit_started",
        )
        for name in state_names:
            setattr(shell, name, False)

        # TARGET mode with no candidate/card is deliberately not represented
        # by any of these lifecycle fields.
        self.assertFalse(shell.custom_target_info_presented(True))
        for name in state_names:
            setattr(shell, name, 1)
            self.assertTrue(
                shell.custom_target_info_presented(True),
                f"{name} did not claim Combat Log presentation",
            )
            self.assertFalse(shell.custom_target_info_presented(False))
            setattr(shell, name, False)

    def test_all_session_combat_log_suppression_is_retired(self):
        self.assertFalse(live.HIDE_NATIVE_COMBAT_LOG)

    def test_the_published_log_shift_is_the_configured_rise(self):
        self.assertEqual(
            struct.unpack("<i", struct.pack("<I", live.COMBAT_LOG_MENU_SHIFT_COOP))[0],
            -live.COMBAT_LOG_COOP_RISE_PX,
        )

    def test_the_lane_can_clear_one_extra_log_line(self):
        self.assertGreater(live.COMBAT_LOG_LINE_PX, 0)

    def test_steal_is_known_to_wrap_its_log_message(self):
        """Battle pack section 2 holds the log copy, and the item-steal and
        failed-steal messages carry a line break of their own.  Keyed by
        action ID because the action rules never carried a name to match on:
        the earlier name lookup silently answered no to every action."""

        self.assertIn(0xA9, live.TWO_LINE_ACTION_IDS)
        self.assertNotIn("name", {field.name for field in fields(ActionRules)})

    def test_the_older_published_rise_is_still_recognised_as_ours(self):
        self.assertIn(
            live.COMBAT_LOG_MENU_SHIFT_PREVIOUS_RISE,
            live.COMBAT_LOG_MENU_SHIFT_MANAGED_VALUES,
        )

    def test_custom_info_lanes_rise_with_combat_log(self):
        self.assertEqual(live.P1_INFO_TOP_Y, 145 - live.COMBAT_LOG_COOP_RISE_PX)
        self.assertEqual(live.P2_INFO_TOP_Y, live.P1_INFO_TOP_Y)
        self.assertEqual(live.TARGET_INFO_Y, live.P1_TARGET_INFO_Y)

    def test_p2_help_has_compact_and_target_clear_lanes(self):
        self.assertLess(live.P1_HELP_Y, live.P2_HELP_COMPACT_Y)
        self.assertEqual(live.P2_HELP_COMPACT_Y, live.P1_HELP_Y + 54)
        self.assertEqual(live.HELP_PANEL_X_COMPENSATION, -3)
        self.assertEqual(live.HELP_PANEL_Y_COMPENSATION, -2)
        self.assertGreaterEqual(
            live.P2_HELP_TARGET_CLEAR_Y,
            live.P1_TARGET_INFO_Y + live.TARGET_INFO_PANEL_HEIGHT,
        )

    def test_p2_target_info_can_occupy_or_vacate_p1_slot(self):
        self.assertEqual(
            live.P2_TARGET_INFO_PRIMARY_X,
            live.P1_TARGET_INFO_X,
        )
        # The secondary slot sits one lane to the right of the primary one.
        # It used to be the constant 390; the lane width now comes from the
        # card so it survives the card being rescaled.
        self.assertGreater(live.world_overlay.panel_pitch(), 0)
        self.assertEqual(
            live.P2_TARGET_INFO_HELP_CLEARANCE_Y,
            live.P2_HELP_COMPACT_Y - live.P1_HELP_Y,
        )

    def test_info_fade_advances_in_both_directions(self):
        entering = live.advance_info_opacity(0.0, True, live.INFO_FADE_SECONDS / 2)
        leaving = live.advance_info_opacity(entering, False, live.INFO_FADE_SECONDS / 4)
        self.assertAlmostEqual(entering, 0.5)
        self.assertAlmostEqual(leaving, 0.25)

    def test_layout_axis_moves_without_overshoot(self):
        moved = live.smooth_layout_axis(62.0, 390.0, 1.0 / 60.0)
        self.assertGreater(moved, 62.0)
        self.assertLess(moved, 390.0)

    def test_vertical_clearance_is_acquired_without_crossing_visible_text(self):
        """A newly visible native owner must win before the next overlay draw."""

        self.assertEqual(
            live.clearance_layout_axis(
                -float(live.UPPER_INFO_IDLE_LIFT_PX),
                float(live.COMBAT_LOG_CLEARANCE_PX),
                1.0 / 120.0,
            ),
            float(live.COMBAT_LOG_CLEARANCE_PX),
        )

    def test_vertical_clearance_is_released_smoothly(self):
        released = live.clearance_layout_axis(
            float(live.COMBAT_LOG_CLEARANCE_PX),
            -float(live.UPPER_INFO_IDLE_LIFT_PX),
            1.0 / 120.0,
        )
        self.assertLess(released, float(live.COMBAT_LOG_CLEARANCE_PX))
        self.assertGreater(released, -float(live.UPPER_INFO_IDLE_LIFT_PX))

    def test_help_draw_clamps_to_clearance_on_log_visibility_edge(self):
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.upper_info_layout_offset_y = -float(live.UPPER_INFO_IDLE_LIFT_PX)
        shell.combat_log_on_screen = True

        self.assertEqual(
            shell.upper_info_y(live.P1_HELP_Y),
            live.P1_HELP_Y
            + live.COMBAT_LOG_CLEARANCE_PX
            + live.COMBAT_LOG_ACTIVE_INFO_NUDGE_PX,
        )

        # Target Info deliberately keeps its existing native-gated animation.
        shell.target_info_extra_offset_y = 0.0
        self.assertEqual(
            shell.target_info_y(live.P1_TARGET_INFO_Y),
            live.P1_TARGET_INFO_Y - live.UPPER_INFO_IDLE_LIFT_PX,
        )

    def test_visible_log_adds_explicit_battle_info_breathing_room(self):
        self.assertEqual(live.COMBAT_LOG_ACTIVE_INFO_NUDGE_PX, 20)
        self.assertLess(
            live.COMBAT_LOG_ACTIVE_INFO_NUDGE_PX,
            live.COMBAT_LOG_LINE_PX,
        )

    def test_target_info_backdrop_has_a_compact_lower_edge(self):
        self.assertEqual(live.TARGET_BACKDROP_BOTTOM_PADDING, 4)

    def test_native_authored_alpha_controls_combat_log_visibility(self):
        process = FakeVisibilityProcess(0)
        tracker = live.NativeCombatLogVisibility(process)
        self.assertFalse(tracker.sample(now=1.0))

        process.alpha = 1
        self.assertTrue(tracker.sample(now=2.0))

        process.alpha = 0x100
        self.assertTrue(tracker.sample(now=2.05))

        process.alpha = 0
        self.assertFalse(tracker.sample(now=2.10))

    def test_idle_information_layout_is_lifted(self):
        self.assertEqual(live.UPPER_INFO_IDLE_LIFT_PX, 40)

    def test_the_lane_block_carries_its_top_padding(self):
        """The padding moves the whole stack, so the pitch between the lanes
        stays exactly what P2_HELP_COMPACT_Y sets it to."""

        self.assertEqual(live.INFO_LANE_TOP_PADDING_PX, 3)
        self.assertEqual(live.P2_HELP_COMPACT_Y - live.P1_HELP_Y, 54)

    def test_the_upper_block_clears_the_log_and_lifts_without_it(self):
        """One offset carries target info and the menu lanes together.

        With the log up the block sits below it; with the log gone it lifts
        into the space the log was using.  It used to sit at the base position
        while the log was up, which is what put these lanes under the log's
        own text.
        """

        self.assertGreater(live.COMBAT_LOG_CLEARANCE_PX, 0)
        self.assertGreaterEqual(
            live.COMBAT_LOG_CLEARANCE_PX, 2 * live.COMBAT_LOG_LINE_PX
        )
        self.assertEqual(live.UPPER_INFO_IDLE_LIFT_PX, 40)

    def test_solo_field_keeps_the_same_raised_log_layout(self):
        """HUD placement must not depend on a materialized P2 actor."""

        solo = SimpleNamespace(p1_gameplay_ready=True, gameplay_ready=False)
        coop = SimpleNamespace(p1_gameplay_ready=True, gameplay_ready=True)
        loading = SimpleNamespace(
            p1_gameplay_ready=False,
            gameplay_ready=False,
        )

        self.assertTrue(live.combat_log_field_layout_allowed(solo))
        self.assertTrue(live.combat_log_field_layout_allowed(coop))
        self.assertFalse(live.combat_log_field_layout_allowed(loading))

    def test_raise_tracks_the_custom_menu_lifecycle(self):
        ready = SimpleNamespace(p1_gameplay_ready=True)
        loading = SimpleNamespace(p1_gameplay_ready=False)

        self.assertFalse(
            live.combat_log_raise_requested(
                ready,
                custom_menu_open=False,
                custom_menu_transition_active=False,
            )
        )
        self.assertTrue(
            live.combat_log_raise_requested(
                ready,
                custom_menu_open=True,
                custom_menu_transition_active=False,
            )
        )
        self.assertTrue(
            live.combat_log_raise_requested(
                ready,
                custom_menu_open=False,
                custom_menu_transition_active=True,
            )
        )
        self.assertFalse(
            live.combat_log_raise_requested(
                loading,
                custom_menu_open=True,
                custom_menu_transition_active=True,
            )
        )

    def test_native_source_shift_is_disabled_once_and_restored(self):
        process = FakeProcess(live.COMBAT_LOG_MENU_SHIFT_NATIVE)
        layout = live.NativeCombatLogLayout(process)

        layout.maintain(True)
        layout.maintain(True)
        self.assertEqual(
            process.dwords[live.COMBAT_LOG_MENU_SHIFT_GLOBAL],
            live.COMBAT_LOG_MENU_SHIFT_COOP,
        )
        self.assertEqual(
            process.writes,
            [
                (
                    live.COMBAT_LOG_MENU_SHIFT_GLOBAL,
                    live.COMBAT_LOG_MENU_SHIFT_COOP,
                )
            ],
        )
        self.assertTrue(layout.engaged)

        layout.maintain(False)
        self.assertEqual(
            process.dwords[live.COMBAT_LOG_MENU_SHIFT_GLOBAL],
            live.COMBAT_LOG_MENU_SHIFT_NATIVE,
        )
        self.assertFalse(layout.engaged)

    def test_boss_gauge_holds_log_and_its_authored_motion_at_zero(self):
        process = FakeProcess(live.COMBAT_LOG_MENU_SHIFT_NATIVE)
        layout = live.NativeCombatLogLayout(process)

        layout.maintain(True, boss_gauge_visible=True)
        layout.maintain(True, boss_gauge_visible=True)

        self.assertEqual(
            process.dwords[live.COMBAT_LOG_MENU_SHIFT_GLOBAL],
            live.COMBAT_LOG_MENU_SHIFT_BOSS_STATIC,
        )
        self.assertEqual(
            process.writes,
            [
                (
                    live.COMBAT_LOG_MENU_SHIFT_GLOBAL,
                    live.COMBAT_LOG_MENU_SHIFT_BOSS_STATIC,
                )
            ],
        )
        self.assertTrue(layout.engaged)

        layout.maintain(True, boss_gauge_visible=False)
        self.assertEqual(
            process.dwords[live.COMBAT_LOG_MENU_SHIFT_GLOBAL],
            live.COMBAT_LOG_MENU_SHIFT_COOP,
        )

    def test_orphaned_coop_shift_is_adopted_and_restored_on_shutdown(self):
        process = FakeProcess(live.COMBAT_LOG_MENU_SHIFT_COOP)
        layout = live.NativeCombatLogLayout(process)
        layout.maintain(True)
        self.assertFalse(process.writes)
        layout.restore()
        self.assertEqual(
            process.dwords[live.COMBAT_LOG_MENU_SHIFT_GLOBAL],
            live.COMBAT_LOG_MENU_SHIFT_NATIVE,
        )

    def test_legacy_zero_is_migrated_to_current_coop_shift(self):
        process = FakeProcess(live.COMBAT_LOG_MENU_SHIFT_LEGACY_COOP)
        layout = live.NativeCombatLogLayout(process)

        layout.maintain(True)

        self.assertEqual(
            process.dwords[live.COMBAT_LOG_MENU_SHIFT_GLOBAL],
            live.COMBAT_LOG_MENU_SHIFT_COOP,
        )
        self.assertTrue(layout.engaged)

    def test_legacy_zero_is_healed_outside_field_layout(self):
        process = FakeProcess(live.COMBAT_LOG_MENU_SHIFT_LEGACY_COOP)
        layout = live.NativeCombatLogLayout(process)

        layout.maintain(False)

        self.assertEqual(
            process.dwords[live.COMBAT_LOG_MENU_SHIFT_GLOBAL],
            live.COMBAT_LOG_MENU_SHIFT_NATIVE,
        )
        self.assertFalse(layout.engaged)

    def test_previous_negative_shift_is_migrated(self):
        process = FakeProcess(live.COMBAT_LOG_MENU_SHIFT_PREVIOUS_COOP)
        layout = live.NativeCombatLogLayout(process)

        layout.maintain(True)

        self.assertEqual(
            process.dwords[live.COMBAT_LOG_MENU_SHIFT_GLOBAL],
            live.COMBAT_LOG_MENU_SHIFT_COOP,
        )

    def test_unknown_constant_is_preserved_for_compatibility(self):
        process = FakeProcess(54)
        layout = live.NativeCombatLogLayout(process)
        layout.maintain(True)
        layout.restore()
        self.assertFalse(process.writes)
        self.assertFalse(layout.compatible)


if __name__ == "__main__":
    unittest.main()
