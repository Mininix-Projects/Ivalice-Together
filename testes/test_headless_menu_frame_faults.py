import contextlib
from collections import deque
import io
import inspect
import struct
from types import SimpleNamespace
import unittest
from unittest import mock

import headless_menu_shell as live


def transient_failures():
    return (
        OSError(299, "partial remote sample"),
        struct.error("torn packed sample"),
        ValueError("closed or incoherent frame resource"),
    )


class HeadlessMenuFrameFaultTests(unittest.TestCase):
    @staticmethod
    def bare_shell():
        shell = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        shell.running = True
        shell.frame_io_retry_started = None
        shell.frame_io_retry_signature = None
        shell.frame_io_retry_last_log = None
        shell.frame_subsystem_failures = {}
        return shell

    def test_target_circle_upkeep_precedes_fallible_panel_rasterization(self):
        source = inspect.getsource(live.CoopMenuRuntime.update_frame)
        circle = source.index("self.sync_world_target()")
        p1_panel = source.index("self.paint_p1_custom_menu(p1_visual_visible)")
        p2_panel = source.index("self.paint(p2_visual_visible)")
        p3_panel = source.index("self.paint_p3(p3_visual_visible)")

        self.assertLess(circle, p1_panel)
        self.assertLess(circle, p2_panel)
        self.assertLess(circle, p3_panel)

    def test_input_failure_cannot_strand_a_sound_for_a_later_frame(self):
        shell = self.bare_shell()
        shell.pending_native_sounds = deque()
        shell.battle_menu_sounds = {"p1": True}
        shell.native_sound_player = SimpleNamespace(play=mock.Mock(return_value=True))

        def failing_input(*_args, **_kwargs):
            shell.queue_native_sound(7, player=1)
            raise OSError(299, "input sample failed after navigation")

        shell.input = mock.Mock(side_effect=failing_input)
        with self.assertRaises(OSError):
            shell.input_and_flush_one_native_sound(
                True,
                native_pause_open=False,
            )

        shell.native_sound_player.play.assert_called_once_with(7)
        self.assertEqual(tuple(shell.pending_native_sounds), ())
        self.assertIn(
            "self.input_and_flush_one_native_sound(",
            inspect.getsource(live.CoopMenuRuntime.update_frame),
        )

    def test_busy_native_sound_pump_discards_the_whole_transient_batch(self):
        shell = self.bare_shell()
        shell.pending_native_sounds = deque((1, 2, 3))
        shell.native_sound_player = SimpleNamespace(
            play=mock.Mock(return_value=False)
        )

        shell.flush_one_native_sound()

        shell.native_sound_player.play.assert_called_once_with(1)
        self.assertEqual(tuple(shell.pending_native_sounds), ())
        shell.native_sound_player.play.reset_mock()
        shell.flush_one_native_sound()
        shell.native_sound_player.play.assert_not_called()

    def test_frame_boundary_quarantines_transient_exception_matrix(self):
        for failure in transient_failures():
            with self.subTest(exception=type(failure).__name__):
                shell = self.bare_shell()
                update = mock.Mock(side_effect=(failure, None))
                with (
                    mock.patch.object(
                        live.runtime_shutdown,
                        "requested",
                        return_value=False,
                    ),
                    mock.patch.object(
                        live.time,
                        "monotonic",
                        side_effect=(10.0, 10.25),
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    shell._update_frame_resilient(True, update=update)
                    shell._update_frame_resilient(True, update=update)

                self.assertTrue(shell.running)
                self.assertIsNone(shell.frame_io_retry_started)
                self.assertEqual(update.call_count, 2)

    def test_frame_boundary_still_exposes_structural_runtime_error(self):
        shell = self.bare_shell()
        failure = RuntimeError("incompatible native layout")

        with self.assertRaises(RuntimeError) as caught:
            shell._update_frame_resilient(
                True,
                update=mock.Mock(side_effect=failure),
            )

        self.assertIs(caught.exception, failure)

    def test_broken_diagnostic_stream_and_unprintable_error_are_nonfatal(self):
        class UnprintableValueError(ValueError):
            def __str__(self):
                raise RuntimeError("string conversion failed")

        shell = self.bare_shell()
        update = mock.Mock(side_effect=UnprintableValueError())
        with (
            mock.patch.object(live.runtime_shutdown, "requested", return_value=False),
            mock.patch.object(live.time, "monotonic", return_value=10.0),
            mock.patch.object(
                live.builtins,
                "print",
                side_effect=BrokenPipeError(32, "closed launcher pipe"),
            ),
        ):
            shell._update_frame_resilient(True, update=update)
            live.print("also nonfatal", flush=True)

        self.assertTrue(shell.running)
        self.assertEqual(
            shell.frame_io_retry_signature,
            (None, "<unprintable UnprintableValueError>"),
        )

    def test_actor_rebuild_retains_last_coherent_topology_on_transient_matrix(self):
        for failure in transient_failures():
            with self.subTest(exception=type(failure).__name__):
                shell = self.bare_shell()
                shell.model = SimpleNamespace(mode=live.MenuMode.CLOSED)
                shell.p1_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
                shell.p3_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
                shell.process = object()
                shell.last_actor_refresh = 1.0
                shell.materialized_party_count = 3
                shell.party = [("P1", 11, 101), ("P2", 22, 202), ("P3", 33, 303)]
                shell.active_party = [
                    (0, "P1", 11, 101),
                    (1, "P2", 22, 202),
                    (5, "P3", 33, 303),
                ]
                shell.complete_party_layout = (0, 1, 5, 0xFFFF)
                shell.target_party = [("P1", 11, 101)]
                shell.foes = [("Foe", 44, 404)]
                shell.p1_foes = [("Foe", 44, 404)]
                shell.p3_foes = [("Foe", 44, 404)]
                shell.neutrals = [("Neutral", 55, 505)]
                shell.p1_neutrals = [("Neutral", 55, 505)]
                shell.p3_neutrals = [("Neutral", 55, 505)]
                coherent = {
                    name: getattr(shell, name)
                    for name in (
                        "materialized_party_count",
                        "party",
                        "active_party",
                        "complete_party_layout",
                        "target_party",
                        "foes",
                        "p1_foes",
                        "p3_foes",
                        "neutrals",
                        "p1_neutrals",
                        "p3_neutrals",
                    )
                }

                with (
                    mock.patch.object(live.time, "monotonic", return_value=10.0),
                    mock.patch.object(
                        live.legacy.coop,
                        "resolve_active_actors",
                        side_effect=failure,
                    ),
                ):
                    shell.refresh_actors(snapshot=object())

                for name, value in coherent.items():
                    self.assertIs(getattr(shell, name), value)
                self.assertEqual(shell.last_actor_refresh, 1.0)

    def test_actor_rebuild_does_not_hide_structural_runtime_error(self):
        shell = self.bare_shell()
        shell.model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.p1_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.p3_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.process = object()
        shell.last_actor_refresh = 0.0
        failure = RuntimeError("actor layout contract changed")

        with (
            mock.patch.object(live.time, "monotonic", return_value=10.0),
            mock.patch.object(
                live.legacy.coop,
                "resolve_active_actors",
                side_effect=failure,
            ),
            self.assertRaises(RuntimeError) as caught,
        ):
            shell.refresh_actors(snapshot=object())

        self.assertIs(caught.exception, failure)

    def test_same_generation_partial_actor_scan_is_not_committed(self):
        shell = self.bare_shell()
        shell.model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.p1_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        shell.p3_model = SimpleNamespace(mode=live.MenuMode.CLOSED)
        identity = live.session_state.SessionIdentity(0, 1, 5)
        layout = (0, 1, 5, *([live.session_state.EMPTY_PARTY_ID] * 6))
        snapshot = SimpleNamespace(
            identity=identity,
            battle_info=0x5000,
            current_layout=layout,
            ui_layout=layout,
            p1_owner_party_member_id=0,
            p1_owner_override=None,
        )
        shell.actor_topology_signature = shell._actor_topology_signature(snapshot)
        shell.last_actor_refresh = 1.0
        shell.materialized_party_count = 3
        shell.party = [("Vaan", 11, 101), ("Balthier", 22, 202), ("Penelo", 33, 303)]
        shell.active_party = [
            (0, "Vaan", 11, 101),
            (1, "Balthier", 22, 202),
            (5, "Penelo", 33, 303),
        ]
        coherent_party = shell.party
        coherent_active_party = shell.active_party
        p1_entry = (0, 101, 1001, (0.0, 0.0, 0.0))
        shell.process = SimpleNamespace(u32=lambda _address: 11)

        with (
            mock.patch.object(live.time, "monotonic", return_value=10.0),
            mock.patch.object(
                live.legacy.coop,
                "resolve_active_actors",
                return_value=[p1_entry],
            ),
            mock.patch.object(
                live.legacy.coop,
                "resolve_active_party_actor_slots",
                return_value={0: p1_entry},
            ),
            mock.patch.object(live, "complete_party_layout", return_value=layout),
            mock.patch.object(
                live,
                "targetable_party_actor_slots",
                side_effect=lambda _process, slots, _layout: slots,
            ),
        ):
            shell.refresh_actors(force=True, snapshot=snapshot)

        self.assertIs(shell.party, coherent_party)
        self.assertIs(shell.active_party, coherent_active_party)
        self.assertEqual(shell.materialized_party_count, 3)
        self.assertEqual(shell.last_actor_refresh, 1.0)

    def test_actor_regression_gate_opens_for_a_real_generation_change(self):
        shell = self.bare_shell()
        identity = live.session_state.SessionIdentity(0, 1, 5)
        old_layout = (0, 1, 5, *([live.session_state.EMPTY_PARTY_ID] * 6))
        old_snapshot = SimpleNamespace(
            identity=identity,
            battle_info=0x5000,
            current_layout=old_layout,
            ui_layout=old_layout,
        )
        shell.actor_topology_signature = shell._actor_topology_signature(old_snapshot)
        shell.active_party = [
            (0, "Vaan", 11, 101),
            (1, "Balthier", 22, 202),
            (5, "Penelo", 33, 303),
        ]
        new_layout = (0, 1, *([live.session_state.EMPTY_PARTY_ID] * 7))
        new_snapshot = SimpleNamespace(
            identity=live.session_state.SessionIdentity(0, 1),
            battle_info=0x6000,
            current_layout=new_layout,
            ui_layout=new_layout,
        )

        self.assertFalse(
            shell._actor_topology_sample_regressed(
                new_snapshot,
                [(0, "Vaan", 44, 404), (1, "Balthier", 55, 505)],
            )
        )

    def test_actor_liveness_sample_uses_transient_exception_matrix(self):
        for failure in transient_failures():
            with self.subTest(exception=type(failure).__name__):
                process = SimpleNamespace(u64=mock.Mock(side_effect=failure))
                self.assertIsNone(live.controlled_actor_alive(process, 0x1000))

    def test_actor_liveness_exposes_structural_runtime_error(self):
        failure = RuntimeError("battle-keep layout changed")
        process = SimpleNamespace(u64=mock.Mock(side_effect=failure))
        with self.assertRaises(RuntimeError) as caught:
            live.controlled_actor_alive(process, 0x1000)
        self.assertIs(caught.exception, failure)

    def test_window_message_pump_quarantines_transient_exception_matrix(self):
        message = live.wt.MSG()
        for failure in transient_failures():
            with self.subTest(exception=type(failure).__name__):
                shell = self.bare_shell()
                user32 = SimpleNamespace(
                    PeekMessageW=mock.Mock(side_effect=failure),
                    TranslateMessage=mock.Mock(),
                    DispatchMessageW=mock.Mock(),
                )
                with (
                    mock.patch.object(live.legacy.ui, "user32", user32),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    shell._pump_window_messages(message)

                self.assertIn("windows-message-pump", shell.frame_subsystem_failures)
                user32.TranslateMessage.assert_not_called()
                user32.DispatchMessageW.assert_not_called()

    def test_window_message_budget_prevents_frame_starvation(self):
        shell = self.bare_shell()
        user32 = SimpleNamespace(
            PeekMessageW=mock.Mock(return_value=1),
            TranslateMessage=mock.Mock(),
            DispatchMessageW=mock.Mock(),
        )
        with mock.patch.object(live.legacy.ui, "user32", user32):
            shell._pump_window_messages(live.wt.MSG())

        self.assertEqual(
            user32.PeekMessageW.call_count,
            live.WINDOW_MESSAGE_BUDGET_PER_FRAME,
        )
        self.assertEqual(
            user32.DispatchMessageW.call_count,
            live.WINDOW_MESSAGE_BUDGET_PER_FRAME,
        )

    def test_window_message_pump_exposes_structural_runtime_error(self):
        shell = self.bare_shell()
        failure = RuntimeError("invalid window contract")
        user32 = SimpleNamespace(
            PeekMessageW=mock.Mock(side_effect=failure),
            TranslateMessage=mock.Mock(),
            DispatchMessageW=mock.Mock(),
        )
        with (
            mock.patch.object(live.legacy.ui, "user32", user32),
            self.assertRaises(RuntimeError) as caught,
        ):
            shell._pump_window_messages(live.wt.MSG())

        self.assertIs(caught.exception, failure)

    def test_foreground_sample_fails_closed_for_transient_exception_matrix(self):
        for failure in transient_failures():
            with self.subTest(exception=type(failure).__name__):
                shell = self.bare_shell()
                shell.pid = 123
                with (
                    mock.patch.object(
                        live.legacy.coop,
                        "process_is_foreground",
                        side_effect=failure,
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertFalse(shell._sample_game_foreground())
                self.assertIn("foreground-sample", shell.frame_subsystem_failures)

    def test_foreground_sample_exposes_structural_runtime_error(self):
        shell = self.bare_shell()
        shell.pid = 123
        failure = RuntimeError("foreground API contract changed")
        with (
            mock.patch.object(
                live.legacy.coop,
                "process_is_foreground",
                side_effect=failure,
            ),
            self.assertRaises(RuntimeError) as caught,
        ):
            shell._sample_game_foreground()

        self.assertIs(caught.exception, failure)

    def test_desktop_event_broker_gap_discards_unproven_local_route(self):
        retained = ("p2", live.NATIVE_INPUT_EVENT_COMMAND, 1)
        for failure in transient_failures():
            with self.subTest(exception=type(failure).__name__):
                shell = self.bare_shell()
                shell.native_desktop_pending_events = [retained]
                shell.native_desktop_capture_owner = "p2"
                shell.native_dialog_bridge = SimpleNamespace(
                    drain_desktop_events=mock.Mock(side_effect=failure),
                    event_overflow_count=0,
                )

                with contextlib.redirect_stdout(io.StringIO()):
                    shell._collect_native_desktop_events(
                        foreground=True,
                        desktop_owner="p2",
                    )

                self.assertEqual(shell.native_desktop_pending_events, [])
                self.assertIn(
                    "desktop-event-broker",
                    shell.frame_subsystem_failures,
                )

                shell.native_dialog_bridge.drain_desktop_events.side_effect = None
                shell.native_dialog_bridge.drain_desktop_events.return_value = ()
                shell._collect_native_desktop_events(
                    foreground=True,
                    desktop_owner="p2",
                )
                self.assertNotIn(
                    "desktop-event-broker",
                    shell.frame_subsystem_failures,
                )

    def test_desktop_event_broker_gap_clears_events_after_ownership_change(self):
        shell = self.bare_shell()
        shell.native_desktop_pending_events = [
            ("p2", live.NATIVE_INPUT_EVENT_COMMAND, 1)
        ]
        shell.native_desktop_capture_owner = "p2"
        shell.native_dialog_bridge = SimpleNamespace(
            drain_desktop_events=mock.Mock(side_effect=OSError("torn ring")),
            event_overflow_count=0,
        )

        with contextlib.redirect_stdout(io.StringIO()):
            shell._collect_native_desktop_events(
                foreground=False,
                desktop_owner=None,
            )

        self.assertEqual(shell.native_desktop_pending_events, [])

    def test_desktop_event_broker_exposes_structural_runtime_error(self):
        shell = self.bare_shell()
        shell.native_desktop_pending_events = []
        shell.native_desktop_capture_owner = "p1"
        failure = RuntimeError("native event ring is incoherent")
        shell.native_dialog_bridge = SimpleNamespace(
            drain_desktop_events=mock.Mock(side_effect=failure),
            event_overflow_count=0,
        )

        with self.assertRaises(RuntimeError) as caught:
            shell._collect_native_desktop_events(
                foreground=True,
                desktop_owner="p1",
            )

        self.assertIs(caught.exception, failure)

    def test_failed_panel_heartbeat_retains_transport_and_forces_gdi_matrix(self):
        for failure in transient_failures():
            with self.subTest(exception=type(failure).__name__):
                shell = self.bare_shell()
                transport = SimpleNamespace(
                    enabled=True,
                    touch=mock.Mock(side_effect=failure),
                    close=mock.Mock(),
                )

                def panel():
                    return SimpleNamespace(
                        d3d_transport=transport,
                        d3d_visible=True,
                        _last_d3d_draw_key=(1,),
                    )

                shell.panel = panel()
                second = panel()
                damage = panel()
                shell.overlay_panels = (shell.panel, second)
                shell.live_damage_meter = SimpleNamespace(panel=damage)
                with contextlib.redirect_stdout(io.StringIO()):
                    shell._maintain_panel_transport_heartbeat()

                transport.close.assert_not_called()
                self.assertTrue(transport.enabled)
                for candidate in (shell.panel, second, damage):
                    self.assertIs(candidate.d3d_transport, transport)
                    self.assertFalse(candidate.d3d_visible)
                    self.assertIsNone(candidate._last_d3d_draw_key)
                self.assertIn("panel-heartbeat", shell.frame_subsystem_failures)

    def test_unavailable_panel_mapping_retries_same_transport_with_gdi_fallback(self):
        shell = self.bare_shell()
        transport = SimpleNamespace(
            enabled=False,
            touch=mock.Mock(return_value=False),
            close=mock.Mock(),
        )

        def panel():
            return SimpleNamespace(
                d3d_transport=transport,
                d3d_visible=True,
                _last_d3d_draw_key=(1,),
            )

        shell.panel = panel()
        second = panel()
        damage = panel()
        shell.overlay_panels = (shell.panel, second)
        shell.live_damage_meter = SimpleNamespace(panel=damage)

        with contextlib.redirect_stdout(io.StringIO()):
            shell._maintain_panel_transport_heartbeat()

        transport.touch.assert_called_once_with()
        transport.close.assert_not_called()
        for candidate in (shell.panel, second, damage):
            self.assertIs(candidate.d3d_transport, transport)
            self.assertFalse(candidate.d3d_visible)
            self.assertIsNone(candidate._last_d3d_draw_key)
        self.assertIn("panel-heartbeat", shell.frame_subsystem_failures)

    def test_recovered_panel_mapping_clears_heartbeat_failure(self):
        shell = self.bare_shell()
        transport = SimpleNamespace(touch=mock.Mock(return_value=True))
        shell.panel = SimpleNamespace(d3d_transport=transport)
        shell.frame_subsystem_failures["panel-heartbeat"] = (
            "OSError",
            "temporarily unavailable",
        )

        shell._maintain_panel_transport_heartbeat()

        transport.touch.assert_called_once_with()
        self.assertNotIn("panel-heartbeat", shell.frame_subsystem_failures)

    def test_panel_mapping_exposes_structural_runtime_error(self):
        shell = self.bare_shell()
        failure = RuntimeError("panel ABI mismatch")
        transport = SimpleNamespace(
            enabled=True,
            touch=mock.Mock(side_effect=failure),
            close=mock.Mock(),
        )
        shell.panel = SimpleNamespace(d3d_transport=transport)

        with self.assertRaises(RuntimeError) as caught:
            shell._maintain_panel_transport_heartbeat()

        self.assertIs(caught.exception, failure)
        self.assertTrue(transport.enabled)
        transport.close.assert_not_called()

    def test_overlay_window_failure_does_not_skip_healthy_panels(self):
        for failure in transient_failures():
            with self.subTest(exception=type(failure).__name__):
                shell = self.bare_shell()
                failed = SimpleNamespace(
                    set_game_foreground=mock.Mock(side_effect=failure)
                )
                healthy = SimpleNamespace(set_game_foreground=mock.Mock())
                shell.overlay_panels = (failed, healthy)
                with contextlib.redirect_stdout(io.StringIO()):
                    shell._set_overlay_foreground_resilient(True)

                failed.set_game_foreground.assert_called_once_with(True)
                healthy.set_game_foreground.assert_called_once_with(True)
                self.assertIn("overlay-foreground", shell.frame_subsystem_failures)

    def test_overlay_window_structural_failure_is_exposed(self):
        shell = self.bare_shell()
        failure = RuntimeError("overlay contract changed")
        panel = SimpleNamespace(set_game_foreground=mock.Mock(side_effect=failure))
        shell.overlay_panels = (panel,)

        with self.assertRaises(RuntimeError) as caught:
            shell._set_overlay_foreground_resilient(True)

        self.assertIs(caught.exception, failure)

    def test_party_intent_publish_retries_same_payload_for_transient_matrix(self):
        identity = live.session_state.SessionIdentity(0, 5, 1)
        captured = ((0, 5, 1), 0x500000, "p3")
        world = (captured[1], 33)
        expected_publish = mock.call(
            captured[0],
            battle_info=captured[1],
            source=captured[2],
        )
        snapshot = SimpleNamespace(
            native_menu_open=False,
            native_modal_active=False,
            battle_info=captured[1],
            identity=identity,
            coherent_world_signature=world,
        )

        for failure in transient_failures():
            with self.subTest(exception=type(failure).__name__):
                shell = self.bare_shell()
                shell.process = object()
                shell.pending_party_intent_publish = None
                shell.party_edit_capture = SimpleNamespace(
                    observe=mock.Mock(return_value=captured)
                )
                shell.party_intent_publisher = SimpleNamespace(
                    publish=mock.Mock(
                        side_effect=(failure, SimpleNamespace(epoch=17))
                    )
                )

                with (
                    mock.patch.object(
                        live,
                        "coherent_manual_party_members",
                        return_value=captured[0],
                    ) as sample_members,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    shell.maintain_party_edit_capture(snapshot, now=1.0)
                    shell.maintain_party_edit_capture(snapshot, now=2.0)

                self.assertEqual(
                    shell.party_intent_publisher.publish.call_args_list,
                    [expected_publish, expected_publish],
                )
                shell.party_edit_capture.observe.assert_called_once_with(
                    native_frontend_open=False,
                    field_stable=True,
                    battle_info=captured[1],
                    active_members=captured[0],
                    now=1.0,
                )
                sample_members.assert_called_once_with(shell.process, snapshot)
                self.assertIsNone(shell.pending_party_intent_publish)

    def test_pending_party_intent_is_discarded_across_world_or_session(self):
        captured_identity = live.session_state.SessionIdentity(0, 5, 1)
        captured = ((0, 5, 1), 0x500000, "p2")
        captured_world = (captured[1], 33)
        changed_identity = live.session_state.SessionIdentity(0, 1, 5)
        cases = (
            (0x600000, captured_identity, (0x600000, 33), "battle generation"),
            (captured[1], changed_identity, captured_world, "role identity"),
            (captured[1], captured_identity, (captured[1], 34), "world"),
            (0, captured_identity, None, "loading boundary"),
        )

        for battle_info, identity, world, reason in cases:
            with self.subTest(reason=reason):
                shell = self.bare_shell()
                shell.pending_party_intent_publish = (
                    captured,
                    captured_identity,
                    captured_world,
                )
                shell.party_intent_publisher = SimpleNamespace(
                    publish=mock.Mock()
                )
                snapshot = SimpleNamespace(
                    battle_info=battle_info,
                    identity=identity,
                    coherent_world_signature=world,
                )

                shell.maintain_party_edit_capture(snapshot, now=2.0)

                shell.party_intent_publisher.publish.assert_not_called()
                self.assertIsNone(shell.pending_party_intent_publish)

    def test_party_intent_publish_exposes_structural_runtime_error(self):
        identity = live.session_state.SessionIdentity(0, 5, 1)
        captured = ((0, 5, 1), 0x500000, "p1")
        world = (captured[1], 44)
        failure = RuntimeError("party journal contract incompatible")
        shell = self.bare_shell()
        shell.process = object()
        shell.pending_party_intent_publish = None
        shell.party_edit_capture = SimpleNamespace(
            observe=mock.Mock(return_value=captured)
        )
        shell.party_intent_publisher = SimpleNamespace(
            publish=mock.Mock(side_effect=failure)
        )
        snapshot = SimpleNamespace(
            native_menu_open=False,
            native_modal_active=False,
            battle_info=captured[1],
            identity=identity,
            coherent_world_signature=world,
        )

        with (
            mock.patch.object(
                live,
                "coherent_manual_party_members",
                return_value=captured[0],
            ),
            self.assertRaises(RuntimeError) as caught,
        ):
            shell.maintain_party_edit_capture(snapshot, now=1.0)

        self.assertIs(caught.exception, failure)
        self.assertEqual(
            shell.pending_party_intent_publish,
            (captured, identity, world),
        )

    def test_shutdown_revokes_pending_party_transactions(self):
        shell = self.bare_shell()
        shell._runtime_cleanup_complete = False
        shell.pending_party_intent_publish = (
            ((0, 5, 1), 0x500000, "p2"),
            live.session_state.SessionIdentity(0, 5, 1),
            (0x500000, 33),
        )
        shell.native_party_menu_request_pending = True
        shell.native_party_menu_request_pulsed = True
        shell.native_party_menu_request_source = "p2"
        shell.native_party_menu_request_deadline = 99.0
        shell.native_party_command_drain_pending = True
        shell.party_edit_capture = SimpleNamespace(cancel=mock.Mock())
        shell.native_combat_log_target_gate = None
        shell.clear_world_target = mock.Mock()
        shell.clear_d3d_target_rings = mock.Mock()
        shell.publish_p1_help_badge = mock.Mock()
        shell.cancel_p1_leader_handoff = mock.Mock()
        shell.session_mutex = None

        shell._cleanup_after_run()

        self.assertIsNone(shell.pending_party_intent_publish)
        self.assertFalse(shell.native_party_menu_request_pending)
        self.assertFalse(shell.native_party_menu_request_pulsed)
        self.assertEqual(shell.native_party_menu_request_source, "")
        self.assertEqual(shell.native_party_menu_request_deadline, 0.0)
        self.assertFalse(shell.native_party_command_drain_pending)
        shell.party_edit_capture.cancel.assert_called_once_with()

    def test_party_menu_request_is_durable_before_transient_suspend_matrix(self):
        for failure in transient_failures():
            with self.subTest(exception=type(failure).__name__):
                shell = self.bare_shell()
                shell.native_party_menu_request_pending = False
                shell.native_party_menu_request_pulsed = True
                shell.native_party_menu_request_source = ""
                shell.native_party_menu_request_deadline = 0.0
                shell.native_party_command_drain_pending = True
                shell.suspend_custom_menus_for_native_frontend = mock.Mock(
                    side_effect=failure
                )
                shell._publish_native_input_contract = mock.Mock()
                shell._clear_native_desktop_route = mock.Mock()
                shell.native_wait_mode_bridge = SimpleNamespace(maintain=mock.Mock())

                with self.assertRaises(type(failure)):
                    shell.request_native_party_menu("p2+p3", 10.0)

                self.assertTrue(shell.native_party_menu_request_pending)
                self.assertFalse(shell.native_party_menu_request_pulsed)
                self.assertEqual(shell.native_party_menu_request_source, "p2+p3")
                self.assertGreater(shell.native_party_menu_request_deadline, 10.0)
                self.assertFalse(shell.native_party_command_drain_pending)
                shell._publish_native_input_contract.assert_not_called()

    def test_party_menu_request_resumes_after_native_contract_gap(self):
        shell = self.bare_shell()
        shell.native_party_menu_request_pending = False
        shell.native_party_menu_request_pulsed = False
        shell.native_party_menu_request_source = ""
        shell.native_party_menu_request_deadline = 0.0
        shell.native_party_command_drain_pending = False
        shell.suspend_custom_menus_for_native_frontend = mock.Mock()
        shell._publish_native_input_contract = mock.Mock(
            side_effect=OSError("native mailbox temporarily unavailable")
        )
        shell._clear_native_desktop_route = mock.Mock()
        shell.native_wait_mode_bridge = SimpleNamespace(
            maintain=mock.Mock(return_value=True),
            lifecycle_close_complete=mock.Mock(return_value=True),
        )

        with self.assertRaises(OSError):
            shell.request_native_party_menu("p3", 10.0)

        self.assertTrue(shell.native_party_menu_request_pending)
        shell._publish_native_input_contract.side_effect = None
        shell.p1_native_lifecycle = mock.Mock(return_value=None)
        shell.native_dialog_bridge = SimpleNamespace(pulse=mock.Mock())
        shell.custom_ui_suppressed = False
        snapshot = SimpleNamespace(
            native_menu_open=False,
            native_modal_active=False,
            loading=False,
            cutscene=False,
            user_control_enabled=True,
            town=False,
        )

        self.assertTrue(
            shell.maintain_native_party_menu_request(
                foreground=True,
                snapshot=snapshot,
                native_pause_open=False,
                now=10.1,
            )
        )

        shell.native_dialog_bridge.pulse.assert_called_once_with(
            live.NATIVE_DIALOG_PARTY_MENU
        )
        self.assertTrue(shell.native_party_menu_request_pulsed)

    def test_session_gate_signature_commits_only_after_all_closes(self):
        for failure in transient_failures():
            with self.subTest(exception=type(failure).__name__):
                shell = self.bare_shell()
                shell.process = None
                shell.base = None
                shell.native_frontend_custom_suspended = False
                shell.party = [("P1", 1, 11), ("P2", 2, 22), ("P3", 3, 33)]
                shell.last_session_gate_signature = None
                shell.hard_close_p1_menu = mock.Mock(side_effect=(failure, None))
                shell.hard_close_p2_menu = mock.Mock()
                shell.hard_close_p3_menu = mock.Mock()
                shell.native_p1_last_handle = 1
                shell.native_p1_last_handle_time = 1.0
                shell.native_p2_last_handle = 2
                shell.native_p2_last_handle_time = 1.0
                shell.native_p3_last_handle = 3
                shell.native_p3_last_handle_time = 1.0
                shell.p1_world_interaction_last = (2, 1, 0, 0)
                shell.p1_world_interaction_last_time = 1.0
                snapshot = SimpleNamespace(
                    p1_menu_allowed=False,
                    p2_menu_allowed=False,
                    identity=live.session_state.SessionIdentity(0, 1, 5),
                    bridge_ownership_allowed=False,
                    loading=True,
                    cutscene=False,
                    town=False,
                    native_menu_open=False,
                    native_modal_active=False,
                    leader_party_member_id=0,
                    camera_allowed=True,
                    reason="loading",
                )

                with self.assertRaises(type(failure)):
                    shell.apply_session_gates(snapshot)
                self.assertIsNone(shell.last_session_gate_signature)

                shell.apply_session_gates(snapshot)
                self.assertIsNotNone(shell.last_session_gate_signature)
                self.assertEqual(shell.hard_close_p1_menu.call_count, 2)
                shell.hard_close_p2_menu.assert_called_once_with()
                shell.hard_close_p3_menu.assert_called_once_with()

    def test_session_gate_structural_failure_does_not_commit(self):
        shell = self.bare_shell()
        shell.process = None
        shell.base = None
        shell.native_frontend_custom_suspended = False
        shell.party = []
        shell.last_session_gate_signature = None
        failure = RuntimeError("native menu layout incompatible")
        shell.hard_close_p1_menu = mock.Mock(side_effect=failure)
        shell.hard_close_p2_menu = mock.Mock()
        shell.hard_close_p3_menu = mock.Mock()
        snapshot = SimpleNamespace(
            p1_menu_allowed=False,
            p2_menu_allowed=False,
            identity=live.session_state.SessionIdentity(),
            bridge_ownership_allowed=False,
            loading=True,
            cutscene=False,
            town=False,
            native_menu_open=False,
            native_modal_active=False,
            leader_party_member_id=0,
            camera_allowed=True,
            reason="loading",
        )

        with self.assertRaises(RuntimeError) as caught:
            shell.apply_session_gates(snapshot)

        self.assertIs(caught.exception, failure)
        self.assertIsNone(shell.last_session_gate_signature)


if __name__ == "__main__":
    unittest.main()
