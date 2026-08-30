import ctypes as ct
import struct
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import d3d_panel_transport as transport
import headless_menu_core as panel_core


ROOT = Path(__file__).resolve().parents[1] / "source"
CPP_SOURCE = (
    Path(__file__).resolve().parents[1] / "fontes_nativos" / "p2_menu_color_dx11.cpp"
).read_text(encoding="utf-8")
PANEL_SOURCE = (ROOT / "headless_menu_core.py").read_text(encoding="utf-8")
TRANSPORT_SOURCE = (ROOT / "d3d_panel_transport.py").read_text(encoding="utf-8")


class D3DPanelTransportTests(unittest.TestCase):
    def test_python_and_cpp_share_the_exact_binary_layout(self):
        self.assertEqual(transport.MAGIC, 0x33504955)
        self.assertEqual(transport.HEADER_SIZE, 192)
        self.assertEqual(transport.PANEL_COUNT, 17)
        self.assertEqual(transport.SLOT_HEADER_SIZE, 64)
        self.assertEqual(
            transport.SLOT_SIZE,
            transport.SLOT_HEADER_SIZE + transport.PIXEL_CAPACITY,
        )
        # The renderer rejects the whole mapping when panel_count disagrees, so
        # a Python-only slot addition silently blanks every overlay. Pin both
        # sides to the same literal instead of only asserting the Python one.
        self.assertIn(f"kPanelCount = {transport.PANEL_COUNT};", CPP_SOURCE)
        self.assertIn(
            "static_assert(sizeof(SharedPanelHeader) == kPanelHeaderSize", CPP_SOURCE
        )
        self.assertIn(
            "static_assert(sizeof(SharedPanelSlot) == kPanelSlotHeaderSize", CPP_SOURCE
        )
        self.assertIn("kPanelMagic = 0x33504955u", CPP_SOURCE)
        self.assertIn("kPanelSlotHeaderSize = 64", CPP_SOURCE)

    def test_premultiplication_matches_layered_window_pixels(self):
        image = Image.new("RGBA", (1, 1), (100, 50, 25, 128))
        self.assertEqual(
            transport.premultiplied_bgra(image),
            bytes((12, 25, 50, 128)),
        )
        self.assertEqual(
            transport.premultiplied_bgra(image, 0.5),
            bytes((6, 12, 25, 64)),
        )

    def test_dynamic_layers_match_full_pillow_composite_exactly(self):
        base = Image.new("RGBA", (8, 6), (20, 40, 80, 180))
        layer = Image.new("RGBA", (3, 2), (200, 100, 50, 128))
        expected = base.copy()
        expected.alpha_composite(layer, (2, 3))
        optimized = expected.copy()
        optimized.info["ff12_dynamic_layers"] = (
            base,
            ((layer, 2, 3),),
        )
        self.assertEqual(
            transport.premultiplied_bgra(optimized),
            transport.premultiplied_bgra(expected),
        )

    def test_all_live_panels_have_deterministic_z_slots(self):
        self.assertEqual(
            tuple(transport.PANEL_SLOTS),
            (
                "world_target",
                "interaction_prompt_p2",
                "menu_p2",
                "help_p2",
                "target_info_backdrop",
                "target_info_p2",
                "target_info_p1",
                "menu_p1",
                "help_p1",
                "menu_p3",
                "help_p3",
                "target_info_p3",
                "interaction_prompt_p3",
                "party_target_cursor_p1",
                "party_target_cursor_p2",
                "party_target_cursor_p3",
                "damage_meter",
            ),
        )
        self.assertEqual(
            tuple(transport.PANEL_SLOTS.values()),
            tuple(range(transport.PANEL_COUNT)),
        )

    def test_renderer_uses_coherent_generations_and_stale_guard(self):
        self.assertIn("sequence_before & 1", CPP_SOURCE)
        self.assertIn("sequence_before != sequence_after", CPP_SOURCE)
        self.assertIn("kPanelPublisherStaleMs", CPP_SOURCE)
        self.assertIn("acknowledged_generation[index]", CPP_SOURCE)
        self.assertIn("DXGI_FORMAT_B8G8R8A8_UNORM", CPP_SOURCE)
        self.assertIn("D3D11_BLEND_ONE", CPP_SOURCE)
        self.assertIn(
            "render_shared_panels(new_rtv, panel_viewport, panels_alive)",
            CPP_SOURCE,
        )
        self.assertIn("panel_reference_viewport(g_width, g_height)", CPP_SOURCE)

    def test_owned_wait_tree_does_not_pulse_every_overlay_off(self):
        self.assertIn("static bool owned_wait_presentation", CPP_SOURCE)
        self.assertIn("kPresentationSuppressedOffset = 0x8600u", CPP_SOURCE)
        self.assertIn("kWaitMenuAppliedOffset = 0x251u", CPP_SOURCE)
        self.assertIn("kNativeCommandActor = 0x02092730u", CPP_SOURCE)
        self.assertIn("kNativeCommandFlag = 0x02092734u", CPP_SOURCE)
        suppression = CPP_SOURCE[
            CPP_SOURCE.index(
                "static bool presentation_overlays_suppressed"
            ) : CPP_SOURCE.index("static constexpr uint32_t party_hud_character_order")
        ]
        self.assertIn("!owned_wait_presentation(cave)", suppression)

    def test_present_heartbeat_precedes_intentional_overlay_suppression(self):
        render = CPP_SOURCE[
            CPP_SOURCE.index("static void render_color_pass") : CPP_SOURCE.index(
                "static HRESULT STDMETHODCALLTYPE hooked_present"
            )
        ]
        panel_heartbeat = render.index("panel_publisher_alive()")
        suppression = render.index("presentation_overlays_suppressed")
        self.assertLess(panel_heartbeat, suppression)

        present = CPP_SOURCE[
            CPP_SOURCE.index("static HRESULT STDMETHODCALLTYPE hooked_present") :
            CPP_SOURCE.index("static LRESULT CALLBACK dummy_window_proc")
        ]
        heartbeat = present.index("publish_camera_frame_for_present")
        overlay_fault_gate = present.index("g_render_faulted")
        self.assertLess(heartbeat, overlay_fault_gate)
        self.assertNotIn("kRingPresentCountOffset", render)

    def test_camera_bridge_uses_one_atomic_snapshot_and_high_resolution_clock(self):
        camera = CPP_SOURCE[
            CPP_SOURCE.index("enum class CameraGoalReadResult") : CPP_SOURCE.index(
                "static bool valid_actor_for_handle"
            )
        ]
        self.assertIn("float collision_exclusion[2]", CPP_SOURCE)
        self.assertIn("sizeof(CameraOutputPayload) == 0x20", CPP_SOURCE)
        self.assertIn("QueryPerformanceCounter(&counter)", camera)
        self.assertIn("QueryPerformanceFrequency(&value)", camera)
        self.assertNotIn("GetTickCount64()", camera)
        self.assertIn("InterlockedExchange64", camera)
        self.assertIn("(uint64_t{1} << 32) | next_generation", camera)
        self.assertNotIn("publish_legacy_camera", CPP_SOURCE)
        self.assertNotIn("clear_legacy_camera_payload", CPP_SOURCE)
        self.assertNotIn("kCameraCommandFocusOffset", CPP_SOURCE)

    def test_camera_present_is_filtered_and_has_a_nonblocking_owner_guard(self):
        present = CPP_SOURCE[
            CPP_SOURCE.index("static HRESULT STDMETHODCALLTYPE hooked_present") :
            CPP_SOURCE.index("static LRESULT CALLBACK dummy_window_proc")
        ]
        self.assertIn("is_game_swapchain(swapchain)", present)
        self.assertIn(
            "InterlockedCompareExchange(&g_camera_publish_busy, 1, 0)",
            present,
        )
        publish = present.index("publish_camera_frame_for_present")
        release = present.index("InterlockedExchange(&g_camera_publish_busy, 0)")
        overlay_fault_gate = present.index("g_render_faulted")
        self.assertLess(publish, release)
        self.assertLess(release, overlay_fault_gate)

    def test_renderer_maps_uip3_only_at_explicit_high_addresses(self):
        start = CPP_SOURCE.index("static uint8_t* map_panel_view_high")
        end = CPP_SOURCE.index("static bool panel_publisher_alive", start)
        mapping_code = CPP_SOURCE[start:end]

        self.assertIn(
            "kPanelMinimumViewAddress = 0x0000000100000000ull",
            CPP_SOURCE,
        )
        self.assertGreaterEqual(mapping_code.count("ull,"), 12)
        self.assertIn("MapViewOfFileEx(", mapping_code)
        self.assertIn("panel_view_is_high(view)", mapping_code)
        self.assertIn("UnmapViewOfFile(view)", mapping_code)
        self.assertIn("uint8_t* view = map_panel_view_high(mapping)", mapping_code)
        self.assertNotRegex(mapping_code, r"\bMapViewOfFile\s*\(")

        # Virtual addresses are process-local.  The Python publisher remains
        # free to map the named section wherever its own process has room.
        self.assertIn("candidate = mmap.mmap(", TRANSPORT_SOURCE)

    def test_layered_panel_keeps_automatic_fallback(self):
        self.assertIn("d3d_panel_transport.panel_transport(pid, tag)", PANEL_SOURCE)
        self.assertIn("self.d3d_transport.publish(", PANEL_SOURCE)
        self.assertIn("self.d3d_transport.hide(self.tag)", PANEL_SOURCE)
        self.assertIn("and self.d3d_transport.enabled", PANEL_SOURCE)
        self.assertIn("UpdateLayeredWindow", PANEL_SOURCE)

    def test_slot_metadata_format_stays_64_bytes(self):
        # sequence + the 44-byte metadata payload + 16 reserved bytes
        self.assertEqual(
            struct.calcsize("<I") + struct.calcsize("<IIIIIfffIII") + 16, 64
        )

    def test_position_only_generation_reuses_the_premultiplied_pixel_block(self):
        channel = transport.D3DPanelTransport.__new__(transport.D3DPanelTransport)
        channel.mapping = bytearray(transport.MAPPING_SIZE)
        channel.configured = True
        channel.enabled = True
        channel._closed = False
        channel._next_reopen_at = 0.0
        channel.lock = threading.RLock()
        channel.states = [
            transport.PublishedPanel() for _ in range(transport.PANEL_COUNT)
        ]
        channel.renderer_available = lambda: True
        channel.touch = lambda: True
        image = Image.new("RGBA", (3, 2), (100, 50, 25, 128))

        self.assertTrue(channel.publish("menu_p2", image, 10, 20))
        slot = transport.PANEL_SLOTS["menu_p2"]
        offset = transport.HEADER_SIZE + slot * transport.SLOT_SIZE
        first_pixels = bytes(
            channel.mapping[
                offset + transport.SLOT_HEADER_SIZE : offset
                + transport.SLOT_HEADER_SIZE
                + 24
            ]
        )
        self.assertEqual(struct.unpack_from("<I", channel.mapping, offset + 44)[0], 0)

        self.assertTrue(channel.publish("menu_p2", image, 11, 20))
        self.assertEqual(struct.unpack_from("<I", channel.mapping, offset + 44)[0], 1)
        self.assertEqual(
            bytes(
                channel.mapping[
                    offset + transport.SLOT_HEADER_SIZE : offset
                    + transport.SLOT_HEADER_SIZE
                    + 24
                ]
            ),
            first_pixels,
        )

    def test_partial_mapping_initialization_is_closed_before_a_retry(self):
        class TrackingBuffer(bytearray):
            def __init__(self, size):
                super().__init__(size)
                self.closed = False

            def flush(self):
                return None

            def close(self):
                self.closed = True

        failed_mapping = TrackingBuffer(transport.MAPPING_SIZE)
        with (
            mock.patch.object(
                transport.mmap,
                "mmap",
                return_value=failed_mapping,
            ),
            mock.patch.object(
                transport.struct,
                "pack_into",
                side_effect=OSError("mapping initialization failed"),
            ),
        ):
            channel = transport.D3DPanelTransport(7701)

        self.assertTrue(failed_mapping.closed)
        self.assertIsNone(channel.mapping)
        self.assertFalse(channel.enabled)

        recovered_mapping = TrackingBuffer(transport.MAPPING_SIZE)
        with mock.patch.object(
            transport.mmap,
            "mmap",
            return_value=recovered_mapping,
        ):
            self.assertTrue(channel.recover(force=True))

        self.assertIs(channel.mapping, recovered_mapping)
        self.assertTrue(channel.enabled)
        channel.close()
        self.assertTrue(recovered_mapping.closed)

    def test_closed_view_falls_back_then_reopens_with_the_same_transport(self):
        real_mmap = transport.mmap.mmap
        created = []

        def anonymous_mapping(_fileno, size, **_kwargs):
            mapping = real_mmap(-1, size, access=transport.mmap.ACCESS_WRITE)
            created.append(mapping)
            return mapping

        with mock.patch.object(
            transport.mmap,
            "mmap",
            side_effect=anonymous_mapping,
        ):
            channel = transport.D3DPanelTransport(7702)
            original = channel.mapping
            original.close()

            self.assertFalse(channel.touch())
            self.assertIsNone(channel.mapping)
            self.assertFalse(channel.enabled)
            self.assertFalse(channel.recover())
            self.assertEqual(len(created), 1)

            self.assertTrue(channel.recover(force=True))
            self.assertIsNotNone(channel.mapping)
            self.assertIsNot(channel.mapping, original)
            self.assertEqual(len(created), 2)
            self.assertEqual(channel.mapping_epoch, 2)
            channel.close()

    def test_reconnect_hides_every_old_slot_with_noncolliding_generations(self):
        class TrackingBuffer(bytearray):
            def __init__(self, size):
                super().__init__(size)
                self.closed = False

            def flush(self):
                return None

            def close(self):
                self.closed = True

        existing = TrackingBuffer(transport.MAPPING_SIZE)
        struct.pack_into(
            "<IIIIII",
            existing,
            0,
            transport.MAGIC,
            transport.VERSION,
            transport.HEADER_SIZE,
            transport.PANEL_COUNT,
            transport.SLOT_SIZE,
            transport.PIXEL_CAPACITY,
        )
        for slot in range(transport.PANEL_COUNT):
            offset = transport.HEADER_SIZE + slot * transport.SLOT_SIZE
            old_generation = 41 + slot
            struct.pack_into("<I", existing, offset, 2)
            struct.pack_into("<I", existing, offset + 4, old_generation)
            struct.pack_into("<I", existing, offset + 8, 1)
            struct.pack_into(
                "<I",
                existing,
                transport.ACK_GENERATIONS_OFFSET + slot * 4,
                old_generation,
            )

        with mock.patch.object(transport.mmap, "mmap", return_value=existing):
            channel = transport.D3DPanelTransport(7703)

        self.assertEqual(channel.mapping_epoch, 1)
        self.assertEqual(
            struct.unpack_from(
                "<I",
                existing,
                transport.PUBLISHER_READY_OFFSET,
            )[0],
            1,
        )
        for slot, state in enumerate(channel.states):
            offset = transport.HEADER_SIZE + slot * transport.SLOT_SIZE
            old_generation = 41 + slot
            sequence, generation, visible = struct.unpack_from(
                "<III",
                existing,
                offset,
            )
            self.assertEqual(sequence, 2)
            self.assertNotEqual(generation, old_generation)
            self.assertNotEqual(
                transport.D3DPanelTransport._next_generation(generation),
                old_generation,
            )
            self.assertEqual(visible, 0)
            self.assertEqual(state.sequence, 2)
            self.assertEqual(state.generation, generation)
            self.assertFalse(state.visible)
            self.assertEqual(
                struct.unpack_from("<I", existing, offset + 40)[0],
                slot,
            )
            self.assertEqual(
                struct.unpack_from(
                    "<I",
                    existing,
                    transport.ACK_GENERATIONS_OFFSET + slot * 4,
                )[0],
                0,
            )

        channel.renderer_available = lambda: True
        channel.touch = lambda: True
        image = Image.new("RGBA", (1, 1), (1, 2, 3, 255))
        old_renderer_generation = 41 + transport.PANEL_SLOTS["menu_p2"]
        self.assertTrue(channel.publish("menu_p2", image, 0, 0))
        self.assertNotEqual(
            channel.states[transport.PANEL_SLOTS["menu_p2"]].generation,
            old_renderer_generation,
        )
        channel.close()
        self.assertTrue(existing.closed)

    @staticmethod
    def _bare_layered_panel(fake_transport):
        panel = panel_core.LayeredPanel.__new__(panel_core.LayeredPanel)
        panel.tag = "menu_p2"
        panel.game = 123
        panel.hwnd = 456
        panel.visible = False
        panel.d3d_visible = False
        panel.d3d_transport = fake_transport
        panel.width = 0
        panel.height = 0
        panel.bits = None
        panel.bitmap = None
        panel.old_bitmap = None
        panel.screen_dc = None
        panel.memory_dc = None
        panel.source = panel_core.wt.POINT(0, 0)
        panel.blend = panel_core.ui.BLENDFUNCTION(
            panel_core.ui.AC_SRC_OVER,
            0,
            255,
            panel_core.ui.AC_SRC_ALPHA,
        )
        panel._last_draw_key = None
        panel._last_draw_image = None
        panel._last_d3d_draw_key = None
        panel._last_d3d_mapping_epoch = None
        panel._last_d3d_touch = 0.0
        return panel

    def test_layered_hide_revokes_a_pending_slot_even_before_d3d_was_visible(self):
        class PendingTransport:
            enabled = True
            mapping_epoch = 1

            def __init__(self):
                self.hidden = []

            def hide(self, tag):
                self.hidden.append(tag)
                return False

        fake = PendingTransport()
        panel = self._bare_layered_panel(fake)

        panel.hide()

        self.assertEqual(fake.hidden, ["menu_p2"])
        self.assertFalse(panel.d3d_visible)
        self.assertIsNone(panel._last_d3d_mapping_epoch)

    def test_failed_d3d_publish_is_revoked_and_uses_gdi_in_the_same_frame(self):
        class UnavailableTransport:
            enabled = True
            mapping_epoch = 1

            def __init__(self):
                self.calls = []

            def publish(self, *_args):
                self.calls.append("publish")
                return False

            def hide(self, _tag):
                self.calls.append("hide")
                return False

        fake = UnavailableTransport()
        panel = self._bare_layered_panel(fake)
        pixel_buffer = (ct.c_ubyte * 4)()

        def resize(width, height):
            panel.width = width
            panel.height = height
            panel.size = panel_core.ui.SIZE(width, height)
            panel.bits = ct.cast(pixel_buffer, ct.c_void_p)

        panel.resize = resize
        image = Image.new("RGBA", (1, 1), (100, 50, 25, 128))
        with (
            mock.patch.object(
                panel_core.ui.user32,
                "GetClientRect",
                return_value=1,
            ),
            mock.patch.object(
                panel_core.ui.user32,
                "ClientToScreen",
                return_value=1,
            ),
            mock.patch.object(
                panel_core.ui.user32,
                "UpdateLayeredWindow",
                return_value=1,
            ) as update_layered,
            mock.patch.object(
                panel_core.ui.user32,
                "ShowWindow",
                return_value=1,
            ),
        ):
            panel.draw(image, 10, 20)

        self.assertEqual(fake.calls, ["publish", "hide"])
        update_layered.assert_called_once()
        self.assertTrue(panel.visible)
        self.assertFalse(panel.d3d_visible)

    def test_mapping_epoch_change_bypasses_the_d3d_fast_path_immediately(self):
        class ReopenedTransport:
            enabled = True
            mapping_epoch = 2

            def __init__(self):
                self.published = 0

            def publish(self, *_args):
                self.published += 1
                return True

            def hide(self, _tag):
                return True

        fake = ReopenedTransport()
        panel = self._bare_layered_panel(fake)
        image = Image.new("RGBA", (1, 1), (1, 2, 3, 255))
        panel.d3d_visible = True
        panel._last_d3d_draw_key = (id(image), 1, 1, 10.0, 20.0, 1.0)
        panel._last_d3d_mapping_epoch = 1
        panel._last_d3d_touch = time.monotonic()

        panel.draw(image, 10, 20)

        self.assertEqual(fake.published, 1)
        self.assertEqual(panel._last_d3d_mapping_epoch, 2)
        self.assertTrue(panel.d3d_visible)

    def test_layered_constructor_rolls_back_window_and_dc_on_gdi_failure(self):
        with (
            mock.patch.object(panel_core.ui, "game_window", return_value=100),
            mock.patch.object(
                panel_core.d3d_panel_transport,
                "panel_transport",
                return_value=None,
            ),
            mock.patch.object(
                panel_core.ui.kernel32,
                "GetModuleHandleW",
                return_value=200,
            ),
            mock.patch.object(
                panel_core.ui.user32,
                "RegisterClassW",
                return_value=1,
            ),
            mock.patch.object(
                panel_core.ui.user32,
                "CreateWindowExW",
                return_value=300,
            ),
            mock.patch.object(
                panel_core.ui.user32,
                "GetDC",
                return_value=400,
            ),
            mock.patch.object(
                panel_core.ui.gdi32,
                "CreateCompatibleDC",
                return_value=0,
            ),
            mock.patch.object(
                panel_core.ui.user32,
                "ReleaseDC",
                return_value=1,
            ) as release_dc,
            mock.patch.object(
                panel_core.ui.user32,
                "DestroyWindow",
                return_value=1,
            ) as destroy_window,
            mock.patch.object(
                panel_core.ui.user32,
                "UnregisterClassW",
                return_value=1,
            ) as unregister_class,
        ):
            with self.assertRaisesRegex(OSError, "CreateCompatibleDC"):
                panel_core.LayeredPanel(7704, "menu_p2")

        release_dc.assert_called_once_with(None, 400)
        destroy_window.assert_called_once_with(300)
        unregister_class.assert_called_once()

    def test_failed_class_rollback_keeps_the_native_callback_alive(self):
        pid = 7706
        tag = "help_p2"
        instance = 200
        key = (instance, f"FF12HeadlessP2Menu_{tag}_{pid}")
        panel_core._RETIRED_WINDOW_PROCEDURES.pop(key, None)
        try:
            with (
                mock.patch.object(panel_core.ui, "game_window", return_value=100),
                mock.patch.object(
                    panel_core.d3d_panel_transport,
                    "panel_transport",
                    return_value=None,
                ),
                mock.patch.object(
                    panel_core.ui.kernel32,
                    "GetModuleHandleW",
                    return_value=instance,
                ),
                mock.patch.object(
                    panel_core.ui.user32,
                    "RegisterClassW",
                    return_value=1,
                ),
                mock.patch.object(
                    panel_core.ui.user32,
                    "CreateWindowExW",
                    return_value=0,
                ),
                mock.patch.object(
                    panel_core.ui.user32,
                    "UnregisterClassW",
                    return_value=0,
                ),
            ):
                with self.assertRaisesRegex(OSError, "CreateWindowExW"):
                    panel_core.LayeredPanel(pid, tag)

            self.assertIn(key, panel_core._RETIRED_WINDOW_PROCEDURES)
            self.assertIsNotNone(panel_core._RETIRED_WINDOW_PROCEDURES[key])
        finally:
            panel_core._RETIRED_WINDOW_PROCEDURES.pop(key, None)

    def test_layered_resize_keeps_the_old_bitmap_until_replacement_is_selected(self):
        panel = self._bare_layered_panel(None)
        panel.memory_dc = 100
        panel.screen_dc = 200
        panel.bitmap = 300
        panel.old_bitmap = 250
        panel.width = 1
        panel.height = 1
        old_bits = ct.c_void_p(0x1111)
        panel.bits = old_bits

        def create_dib(_dc, _info, _colors, bits, _section, _offset):
            ct.cast(bits, ct.POINTER(ct.c_void_p))[0] = ct.c_void_p(0x2222)
            return 400

        with (
            mock.patch.object(
                panel_core.ui.gdi32,
                "CreateDIBSection",
                side_effect=create_dib,
            ),
            mock.patch.object(
                panel_core.ui.gdi32,
                "SelectObject",
                return_value=300,
            ) as select_object,
            mock.patch.object(
                panel_core.ui.gdi32,
                "DeleteObject",
                return_value=1,
            ) as delete_object,
        ):
            panel.resize(2, 3)

        select_object.assert_called_once_with(100, 400)
        delete_object.assert_called_once_with(300)
        self.assertEqual(panel.bitmap, 400)
        self.assertEqual(panel.old_bitmap, 250)
        self.assertEqual(panel.bits.value, 0x2222)
        self.assertEqual((panel.width, panel.height), (2, 3))

    def test_layered_resize_rolls_back_when_the_old_bitmap_cannot_be_deleted(self):
        panel = self._bare_layered_panel(None)
        panel.memory_dc = 100
        panel.screen_dc = 200
        panel.bitmap = 300
        panel.old_bitmap = 250
        panel.width = 1
        panel.height = 1
        old_bits = ct.c_void_p(0x1111)
        panel.bits = old_bits

        def create_dib(_dc, _info, _colors, bits, _section, _offset):
            ct.cast(bits, ct.POINTER(ct.c_void_p))[0] = ct.c_void_p(0x2222)
            return 400

        with (
            mock.patch.object(
                panel_core.ui.gdi32,
                "CreateDIBSection",
                side_effect=create_dib,
            ),
            mock.patch.object(
                panel_core.ui.gdi32,
                "SelectObject",
                side_effect=(300, 400),
            ) as select_object,
            mock.patch.object(
                panel_core.ui.gdi32,
                "DeleteObject",
                side_effect=(0, 1),
            ) as delete_object,
        ):
            with self.assertRaisesRegex(OSError, "DeleteObject"):
                panel.resize(2, 3)

        self.assertEqual(select_object.call_args_list[-1], mock.call(100, 300))
        self.assertEqual(delete_object.call_args_list[-1], mock.call(400))
        self.assertEqual(panel.bitmap, 300)
        self.assertEqual(panel.old_bitmap, 250)
        self.assertIs(panel.bits, old_bits)
        self.assertEqual((panel.width, panel.height), (1, 1))

    def test_pid_cache_survives_reconnect_and_replaces_only_a_closed_owner(self):
        pid = 0x7F7705
        real_mmap = transport.mmap.mmap

        def anonymous_mapping(_fileno, size, **_kwargs):
            return real_mmap(-1, size, access=transport.mmap.ACCESS_WRITE)

        transport._transports.pop(pid, None)
        try:
            with (
                mock.patch.dict(
                    transport.os.environ,
                    {"FF12_COOP_D3D_PANELS": "1"},
                ),
                mock.patch.object(
                    transport.mmap,
                    "mmap",
                    side_effect=anonymous_mapping,
                ),
            ):
                first = transport.panel_transport(pid, "menu_p2")
                self.assertIsNotNone(first)
                self.assertIs(
                    transport.panel_transport(pid, "help_p3"),
                    first,
                )

                first.invalidate()
                self.assertTrue(first.recover(force=True))
                self.assertIs(
                    transport.panel_transport(pid, "world_target"),
                    first,
                )

                first.close()
                replacement = transport.panel_transport(pid, "menu_p2")
                self.assertIsNotNone(replacement)
                self.assertIsNot(replacement, first)
                replacement.close()
        finally:
            transport._transports.pop(pid, None)


if __name__ == "__main__":
    unittest.main()
