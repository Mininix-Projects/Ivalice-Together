import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import damage_meter as meter
import launcher_core
import menu_themes
import session_state


def snapshot(*, current=(5, 1, 3), ui=None, location=10, loading=False):
    empty = (session_state.EMPTY_PARTY_ID,) * (9 - len(current))
    current_layout = tuple(current) + empty
    ui_layout = current_layout if ui is None else tuple(ui) + empty
    return SimpleNamespace(
        current_layout=current_layout,
        ui_layout=ui_layout,
        current_location=location,
        ui_location=location,
        loading=loading,
    )


class DamageTotalsTests(unittest.TestCase):
    @staticmethod
    def event(sequence: int, amount: int, location: int) -> meter.DamageEvent:
        return meter.DamageEvent(sequence, 0x1000, 0x2000, amount, location)

    def test_area_resets_while_indefinite_survives_location_change(self):
        area = meter.DamageTotals(meter.MODE_AREA)
        indefinite = meter.DamageTotals(meter.MODE_INDEFINITE)
        for totals in (area, indefinite):
            totals.observe_location(10)
            totals.add(self.event(1, 120, 10), 5)
            totals.observe_location(11)
            totals.add(self.event(2, 70, 11), 5)
        self.assertEqual(area.value(5), 70)
        self.assertEqual(indefinite.value(5), 190)

    def test_rows_rank_damage_and_use_party_order_for_ties(self):
        totals = meter.DamageTotals(meter.MODE_INDEFINITE)
        totals.observe_location(1)
        totals.add(self.event(1, 50, 1), 5)
        totals.add(self.event(2, 80, 1), 1)
        totals.add(self.event(3, 50, 1), 3)
        rows = meter.rank_rows((5, 1, 3), totals)
        self.assertEqual([row.member_id for row in rows], [1, 5, 3])

    def test_effective_mode_excludes_overkill(self):
        raw = meter.DamageTotals(meter.MODE_AREA)
        effective = meter.DamageTotals(meter.MODE_AREA, effective_only=True)
        event = meter.DamageEvent(1, 0x1000, 0x2000, 9_999, 10, 300)
        for totals in (raw, effective):
            totals.observe_location(10)
            totals.add(event, 5)
        self.assertEqual(raw.value(5), 9_999)
        self.assertEqual(effective.value(5), 300)

    def test_persisted_session_keeps_raw_and_effective_totals(self):
        event = meter.DamageEvent(1, 0x1000, 0x2000, 900, 10, 125)
        totals = meter.DamageTotals(meter.MODE_INDEFINITE)
        totals.observe_location(10)
        totals.add(event, 5)

        restored_raw = meter.DamageTotals(
            meter.MODE_INDEFINITE,
            indefinite_gross=totals.indefinite_gross,
            indefinite_effective=totals.indefinite_effective,
        )
        restored_effective = meter.DamageTotals(
            meter.MODE_INDEFINITE,
            effective_only=True,
            indefinite_gross=totals.indefinite_gross,
            indefinite_effective=totals.indefinite_effective,
        )
        self.assertEqual(restored_raw.value(5), 900)
        self.assertEqual(restored_effective.value(5), 125)


class DamageSessionStoreTests(unittest.TestCase):
    def test_totals_survive_a_new_runtime_for_the_same_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "damage-session.json"
            store = meter.DamageSessionStore(7, path, save_interval=0.0)
            self.assertTrue(store.update({5: 400}, {5: 275}, now=1.0, force=True))

            restored = meter.DamageSessionStore(7, path)
            self.assertEqual(restored.gross, {5: 400})
            self.assertEqual(restored.effective, {5: 275})

    def test_new_session_id_does_not_reuse_previous_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "damage-session.json"
            meter.DamageSessionStore(4, path, save_interval=0.0).update(
                {1: 999}, {1: 500}, now=1.0, force=True
            )

            reset = meter.DamageSessionStore(5, path)
            self.assertEqual(reset.gross, {})
            self.assertEqual(reset.effective, {})
            reset.update({}, {}, now=2.0, force=True)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["session_id"], 5)


class LivePartyTests(unittest.TestCase):
    def test_actor_ownership_drops_removed_or_unmaterialized_members(self):
        active_party = (
            (5, "Penelo", 10, 0x1000),
            (1, "Ashe", 11, 0x2000),
            (3, "Balthier", 12, 0x3000),
        )
        self.assertEqual(
            meter.active_actor_owners(active_party, (5, 3)),
            {0x1000: 5, 0x3000: 3},
        )

    def test_materialized_actors_remain_authoritative_during_party_reassignment(self):
        active_party = (
            (5, "Penelo", 10, 0x1000),
            (3, "Balthier", 12, 0x3000),
        )
        self.assertEqual(meter.materialized_party_members(active_party), (5, 3))

    def test_render_uses_the_target_info_font_and_requested_themes(self):
        rows = (
            meter.DamageRow(5, 13219, 0),
            meter.DamageRow(1, 8000, 1),
        )
        image = meter.render_meter(rows, menu_themes.ThemeSelection())
        self.assertEqual(image.size, (meter.PANEL_WIDTH, meter.PANEL_HEIGHT))
        self.assertIsNotNone(image.getbbox())


class RowAnimatorTests(unittest.TestCase):
    @staticmethod
    def rows(order):
        return tuple(
            meter.DamageRow(member_id, 100 - index, index)
            for index, member_id in enumerate(order)
        )

    def test_first_layout_is_already_aligned(self):
        animator = meter.RowAnimator()
        layout = animator.layout(self.rows((5, 1, 3)), 10.0)
        self.assertEqual([y for _row, y in layout], [0, 31, 62])

    def test_overtake_animates_both_rows_with_smooth_endpoints(self):
        animator = meter.RowAnimator(duration=0.32)
        animator.layout(self.rows((5, 1, 3)), 1.0)

        started = animator.layout(self.rows((1, 5, 3)), 2.0)
        halfway = animator.layout(self.rows((1, 5, 3)), 2.16)
        finished = animator.layout(self.rows((1, 5, 3)), 2.32)

        self.assertEqual([y for _row, y in started], [31, 0, 62])
        self.assertTrue(all(abs(y - 15.5) <= 0.5 for _row, y in halfway[:2]))
        self.assertEqual(halfway[2][1], 62)
        self.assertEqual([y for _row, y in finished], [0, 31, 62])

    def test_retarget_during_motion_does_not_jump(self):
        animator = meter.RowAnimator(duration=0.32)
        original = self.rows((5, 1, 3))
        swapped = self.rows((1, 5, 3))
        animator.layout(original, 1.0)
        animator.layout(swapped, 2.0)
        before = dict((row.member_id, y) for row, y in animator.layout(swapped, 2.08))
        after = dict((row.member_id, y) for row, y in animator.layout(original, 2.08))
        self.assertEqual(after, before)


class TransitionTests(unittest.TestCase):
    def test_reappears_125_seconds_after_area_transition(self):
        gate = meter.AreaTransitionGate()
        self.assertTrue(gate.ready(snapshot(), 1.0))
        self.assertFalse(gate.ready(snapshot(loading=True), 2.0))
        changed = snapshot(location=11)
        self.assertFalse(gate.ready(changed, 3.0))
        self.assertFalse(gate.ready(changed, 4.24))
        self.assertTrue(gate.ready(changed, 4.25))

    def test_direct_location_change_also_starts_the_delay(self):
        gate = meter.AreaTransitionGate()
        self.assertTrue(gate.ready(snapshot(location=10), 1.0))
        self.assertFalse(gate.ready(snapshot(location=11), 2.0))
        self.assertTrue(gate.ready(snapshot(location=11), 3.25))


class LiveSettingsTests(unittest.TestCase):
    def test_tracker_reloads_launcher_changes_after_its_poll_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.json"
            launcher_core.save_launcher_config({}, path)
            tracker = meter.DamageMeterSettingsTracker(path, poll_seconds=0.05)
            self.assertFalse(tracker.settings.enabled)

            launcher_core.save_launcher_config(
                {
                    "damage_meter_enabled": True,
                    "damage_meter_mode": meter.MODE_INDEFINITE,
                    "damage_meter_effective": True,
                },
                path,
            )
            before_poll = tracker._next_poll - 0.001
            self.assertFalse(tracker.reload(now=before_poll).enabled)
            changed = tracker.reload(now=tracker._next_poll)
            self.assertEqual(
                changed,
                meter.DamageMeterSettings(True, meter.MODE_INDEFINITE, True),
            )

    def test_live_meter_retries_startup_and_reconfigures_without_restart(self):
        class Tracker:
            def __init__(self):
                self.settings = meter.DamageMeterSettings(True)

            def reload(self, *, now):
                return self.settings

        class Panel:
            def __init__(self):
                self.closed = False
                self.foreground = []

            def set_game_foreground(self, foreground):
                self.foreground.append(foreground)

            def close(self):
                self.closed = True

        class Meter:
            def __init__(self, mode, effective_only, session_id):
                self.mode = mode
                self.effective_only = effective_only
                self.session_id = session_id
                self.closed = False
                self.updates = 0

            def update(self, *args, **kwargs):
                self.updates += 1

            def close(self):
                self.closed = True

        tracker = Tracker()
        panels = []
        meters = []
        attempts = 0

        def panel_factory():
            panel = Panel()
            panels.append(panel)
            return panel

        def meter_factory(_process, _panel, mode, *, effective_only, session_id):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("co-op runtime is not active")
            created = Meter(mode, effective_only, session_id)
            meters.append(created)
            return created

        live = meter.LiveDamageMeter(
            object(),
            panel_factory,
            settings_tracker=tracker,
            meter_factory=meter_factory,
        )
        live.update(None, (), None, presentation_visible=True, foreground=True, now=1.0)
        self.assertIsNone(live.meter)
        self.assertTrue(panels[0].closed)

        live.update(None, (), None, presentation_visible=True, foreground=True, now=1.2)
        self.assertEqual(attempts, 1)
        live.update(None, (), None, presentation_visible=True, foreground=True, now=1.5)
        self.assertEqual(attempts, 2)
        self.assertEqual(meters[0].updates, 1)

        tracker.settings = meter.DamageMeterSettings(
            True,
            meter.MODE_INDEFINITE,
            True,
            2,
        )
        live.update(
            None, (), None, presentation_visible=True, foreground=False, now=2.0
        )
        self.assertTrue(meters[0].closed)
        self.assertTrue(panels[1].closed)
        self.assertEqual(meters[1].mode, meter.MODE_INDEFINITE)
        self.assertTrue(meters[1].effective_only)
        self.assertEqual(meters[1].session_id, 2)

        tracker.settings = meter.DamageMeterSettings(False)
        live.update(None, (), None, presentation_visible=True, foreground=True, now=2.5)
        self.assertTrue(meters[1].closed)
        self.assertTrue(panels[2].closed)
        self.assertIsNone(live.meter)


class NativeHookTests(unittest.TestCase):
    def test_meter_accepts_current_and_legacy_movement_cave_layouts(self):
        movement_base = 0x50000000
        theme_base = 0x51000000
        theme_call = b"\xe8" + meter.runtime.rel32(
            meter.runtime.DAMAGE_NUMBER_SOURCE_CALL_SITE,
            5,
            theme_base + meter.runtime.DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET,
        )

        class Process:
            source_call = meter.runtime.DAMAGE_NUMBER_SOURCE_CALL_ORIGINAL
            movement_patch = b""

            def read(self, address, size):
                if address == meter.runtime.MOVEMENT_HOOK_SITE:
                    return self.movement_patch[:size]
                if address == movement_base:
                    return meter.runtime.CAVE_MAGIC[:size]
                if address == meter.runtime.DAMAGE_NUMBER_SOURCE_CALL_SITE:
                    return self.source_call[:size]
                if address == theme_base:
                    return meter.runtime.DAMAGE_NUMBER_THEME_MAGIC[:size]
                raise OSError("unmapped")

        process = Process()
        hook = meter.DamageHook(process)
        for cave_offset in (meter.runtime.MOVEMENT_CAVE_OFFSET, 0x100):
            with self.subTest(cave_offset=hex(cave_offset)):
                process.movement_patch = b"\xe9" + meter.runtime.rel32(
                    meter.runtime.MOVEMENT_HOOK_SITE,
                    5,
                    movement_base + cave_offset,
                )
                process.source_call = meter.runtime.DAMAGE_NUMBER_SOURCE_CALL_ORIGINAL
                with self.assertRaisesRegex(RuntimeError, "theme runtime is not ready"):
                    hook._require_coop_runtime()

                process.source_call = theme_call
                hook._require_coop_runtime()

    def test_wrapper_accepts_negative_damage_and_restores_original_registers(self):
        wrapper_address = 0x50000100
        previous_target = 0x3283D0
        code = meter.build_wrapper(wrapper_address, 0x50000000, previous_target)
        self.assertLess(len(code), meter.RING_OFFSET - meter.HOOK_WRAPPER_OFFSET)
        self.assertEqual(code[:11], b"\x50\x51\x52\x41\x50\x41\x51\x41\x52\x41\x53")
        self.assertIn(b"\x85\xd2\x0f\x8d", code)
        self.assertIn(b"\xf7\xda", code)
        self.assertIn(
            b"\x41\x89\xd0\x44\x3b\x46\x48\x44\x0f\x47\x46\x48",
            code,
        )
        self.assertEqual(code[-27:-16], b"\x41\x5b\x41\x5a\x41\x59\x41\x58\x5a\x59\x58")
        self.assertEqual(code[-16:], meter.build_absolute_jump(previous_target))

    def test_absolute_jump_preserves_rax_and_accepts_a_full_64_bit_target(self):
        target = 0x00007FFEAABBCCDD
        code = meter.build_absolute_jump(target)
        self.assertEqual(code[:3], b"\x50\x48\xb8")
        self.assertEqual(struct.unpack("<Q", code[3:11])[0], target)
        self.assertEqual(code[11:], b"\x48\x87\x04\x24\xc3")

    def test_ring_reader_returns_complete_events(self):
        base = 0x100000
        published = 2
        records = {}
        for sequence, amount in ((1, 120), (2, 340)):
            offset = (
                base
                + meter.RING_OFFSET
                + (sequence & (meter.RING_CAPACITY - 1)) * meter.EVENT_SIZE
            )
            records[offset] = struct.pack(
                "<QQQIIII",
                sequence,
                0x1000 + sequence,
                0x2000 + sequence,
                amount,
                min(amount, 100),
                7,
                0xFFFFFFFF,
            ).ljust(meter.EVENT_SIZE, b"\0")

        class Process:
            def u64(self, address):
                self.assert_address(address)
                return published

            @staticmethod
            def assert_address(address):
                if address != base + meter.HEADER_SEQUENCE_OFFSET:
                    raise AssertionError(f"unexpected sequence address: {address:#x}")

            @staticmethod
            def read(address, size):
                if size != meter.EVENT_SIZE:
                    raise AssertionError(f"unexpected read size: {size}")
                return records[address]

        hook = meter.DamageHook(Process())
        hook.base = base
        sequence, events = hook.read_since(0)
        self.assertEqual(sequence, 2)
        self.assertEqual([event.amount for event in events], [120, 340])
        self.assertEqual([event.effective_amount for event in events], [100, 100])
        self.assertEqual([event.location for event in events], [7, 7])


if __name__ == "__main__":
    unittest.main()
