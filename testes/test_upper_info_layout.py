"""The upper-left information block is placed, not animated.

Target Info hides the Combat Log for as long as it is up, so it never shares the
space and never has to travel to its resting place. Clearance for the blocks
that do share it comes from raising the log at its native source instead of
pushing this block down, which is what the co-op field layout was always for.
"""

import unittest

import headless_menu_shell as live


def layout():
    menu = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
    menu.upper_info_layout_offset_y = 0.0
    menu.upper_info_layout_initialized = False
    menu.target_info_extra_offset_y = 0.0
    menu.upper_info_layout_last_tick = 0.0
    menu.combat_log_on_screen = False
    menu.boss_gauge_visible = False
    return menu


class UpperInfoPlacementTests(unittest.TestCase):
    def test_first_tick_is_already_the_final_position(self):
        """No entry slide: the lifted position is also the starting one."""

        menu = layout()
        menu.update_upper_info_layout()
        self.assertEqual(
            menu.upper_info_layout_offset_y,
            -float(live.UPPER_INFO_IDLE_LIFT_PX),
        )

    def test_position_does_not_depend_on_the_log(self):
        menu = layout()
        menu.update_upper_info_layout()
        with_log = menu.upper_info_layout_offset_y
        menu.update_upper_info_layout()
        self.assertEqual(with_log, menu.upper_info_layout_offset_y)

    def test_visible_native_boss_gauge_drops_the_complete_block(self):
        menu = layout()
        menu.boss_gauge_visible = True
        menu.update_upper_info_layout()
        self.assertEqual(
            menu.upper_info_layout_offset_y,
            -float(live.UPPER_INFO_IDLE_LIFT_PX) + float(live.BOSS_GAUGE_INFO_DROP_PX),
        )

    def test_native_boss_gauge_detector_uses_render_object_flags(self):
        manager = 0x10000
        owner = 0x20000
        gauge = 0x30000

        class FakeProcess:
            def __init__(self, flags):
                self.flags = flags

            def u64(self, address):
                return {
                    live.NATIVE_BOSS_GAUGE_MANAGER: manager,
                    manager + live.NATIVE_BOSS_GAUGE_OWNER_OFFSET: owner,
                    owner + live.NATIVE_BOSS_GAUGE_OBJECT_OFFSETS[0]: gauge,
                    owner + live.NATIVE_BOSS_GAUGE_OBJECT_OFFSETS[1]: 0,
                }.get(address, 0)

            def u32(self, address):
                if address == gauge + live.NATIVE_BOSS_GAUGE_RENDER_FLAGS_OFFSET:
                    return self.flags
                return 0

        self.assertTrue(live.native_boss_gauge_visible(FakeProcess(0x10)))
        self.assertFalse(live.native_boss_gauge_visible(FakeProcess(0x00)))

    def test_repeated_ticks_never_move_it(self):
        """A residual animation would show up as drift across ticks."""

        menu = layout()
        menu.update_upper_info_layout()
        first = menu.upper_info_layout_offset_y
        for _ in range(20):
            menu.update_upper_info_layout()
        self.assertEqual(first, menu.upper_info_layout_offset_y)

    def test_target_card_takes_no_extra_drop(self):
        menu = layout()
        menu.target_info_extra_offset_y = 99.0
        menu.update_upper_info_layout()
        self.assertEqual(menu.target_info_extra_offset_y, 0.0)

    def test_target_info_y_is_the_lifted_base(self):
        menu = layout()
        menu.update_upper_info_layout()
        self.assertEqual(
            menu.target_info_y(200.0),
            200 - live.UPPER_INFO_IDLE_LIFT_PX,
        )

    def test_help_keeps_its_own_clamp(self):
        """Help has no gate of its own, so it may still yield to the log."""

        menu = layout()
        menu.update_upper_info_layout()
        menu.combat_log_on_screen = True
        self.assertEqual(
            menu.upper_info_y(200.0),
            200 + live.COMBAT_LOG_CLEARANCE_PX + live.COMBAT_LOG_ACTIVE_INFO_NUDGE_PX,
        )
        menu.combat_log_on_screen = False
        self.assertEqual(
            menu.upper_info_y(200.0),
            200 - live.UPPER_INFO_IDLE_LIFT_PX,
        )


class CombatLogRaiseTests(unittest.TestCase):
    def test_raise_is_negative_so_the_log_goes_up(self):
        self.assertLess(
            live.COMBAT_LOG_MENU_SHIFT_COOP - (1 << 32),
            0,
        )
        self.assertEqual(
            live.COMBAT_LOG_MENU_SHIFT_COOP,
            (-live.COMBAT_LOG_COOP_RISE_PX) & 0xFFFFFFFF,
        )

    def test_stock_value_is_a_descent(self):
        """55 pushes the log down; leaving it there is the overlap we fix."""

        self.assertGreater(live.COMBAT_LOG_MENU_SHIFT_NATIVE, 0)

    def test_guard_raises_whenever_the_overlay_owns_the_screen(self):
        class FakeProcess:
            def __init__(self):
                self.value = live.COMBAT_LOG_MENU_SHIFT_NATIVE

            def u32(self, address):
                assert address == live.COMBAT_LOG_MENU_SHIFT_GLOBAL
                return self.value

            def write(self, address, payload):
                assert address == live.COMBAT_LOG_MENU_SHIFT_GLOBAL
                self.value = int.from_bytes(payload, "little")

        process = FakeProcess()
        guard = live.NativeCombatLogLayout(process)
        guard.maintain(True)
        self.assertEqual(process.value, live.COMBAT_LOG_MENU_SHIFT_COOP)
        guard.maintain(False)
        self.assertEqual(process.value, live.COMBAT_LOG_MENU_SHIFT_NATIVE)
        guard.restore()
        self.assertEqual(process.value, live.COMBAT_LOG_MENU_SHIFT_NATIVE)


class InfoFadeTests(unittest.TestCase):
    """The fade belongs to closing the menu, not to being replaced by it."""

    def test_appearing_is_never_immediate(self):
        for immediate in (False, True):
            self.assertAlmostEqual(
                live.advance_info_opacity(
                    0.0, True, live.INFO_FADE_SECONDS, immediate=immediate
                ),
                1.0,
            )

    def test_closing_still_fades(self):
        opacity = live.advance_info_opacity(
            1.0, False, live.INFO_FADE_SECONDS / 4, immediate=False
        )
        self.assertGreater(opacity, 0.0)
        self.assertLess(opacity, 1.0)

    def test_any_other_exit_drops_in_one_frame(self):
        self.assertEqual(
            live.advance_info_opacity(1.0, False, 1 / 120, immediate=True),
            0.0,
        )

    def test_immediate_does_not_depend_on_the_frame_length(self):
        for delta in (0.0, 1 / 240, 1 / 60, 1.0):
            self.assertEqual(
                live.advance_info_opacity(1.0, False, delta, immediate=True),
                0.0,
                delta,
            )


if __name__ == "__main__":
    unittest.main()


class CombatLogLatchTests(unittest.TestCase):
    """A first unlucky read must not disable the raise for the session."""

    class Process:
        def __init__(self, sequence):
            self.sequence = list(sequence)
            self.value = self.sequence.pop(0)
            self.writes = []

        def u32(self, address):
            if self.sequence:
                self.value = self.sequence.pop(0)
            return self.value

        def write(self, address, payload):
            self.value = int.from_bytes(payload, "little")
            self.writes.append(self.value)

    def test_it_recovers_after_an_unmanaged_first_sample(self):
        process = self.Process([0xDEADBEEF, 0xDEADBEEF, 55, 55])
        guard = live.NativeCombatLogLayout(process)
        guard.maintain(True)
        self.assertEqual(process.writes, [], "nao deve brigar por um valor alheio")
        guard.maintain(True)
        self.assertEqual(
            process.writes,
            [live.COMBAT_LOG_MENU_SHIFT_COOP],
            "devia assumir assim que o valor voltou a ser gerenciado",
        )

    def test_it_yields_again_if_the_value_stops_being_ours(self):
        process = self.Process([55, 55, 0xDEADBEEF, 0xDEADBEEF])
        guard = live.NativeCombatLogLayout(process)
        guard.maintain(True)
        self.assertEqual(process.writes, [live.COMBAT_LOG_MENU_SHIFT_COOP])
        guard.maintain(True)
        self.assertEqual(len(process.writes), 1, "nao deve sobrescrever outro dono")


class FadeAttributeTests(unittest.TestCase):
    """Each player's transition attribute really exists on the shell.

    The fade reads a transition kind per player, and P2's is the only one
    without a prefix. Naming it p2_transition_kind passed every offline test and
    then killed the headless worker with AttributeError the first time a P2 help
    card was painted, which reads in game as the whole co-op switching off.
    """

    def test_every_player_transition_attribute_is_written_somewhere(self):
        import inspect

        source = inspect.getsource(live.CoopMenuRuntime)
        for nome in ("transition_kind", "p1_transition_kind", "p3_transition_kind"):
            self.assertIn(f"self.{nome} =", source, nome)

    def test_the_fade_does_not_invent_a_p2_prefix(self):
        import inspect

        source = inspect.getsource(live.CoopMenuRuntime)
        self.assertNotIn("self.p2_transition_kind", source)
