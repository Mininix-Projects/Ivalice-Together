import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[1] / "fontes_nativos" / "p2_menu_color_dx11.cpp"
).read_text(encoding="utf-8")


class RendererPartyHudTests(unittest.TestCase):
    def test_present_hook_chains_and_restores_with_compare_exchange(self):
        attempt = SOURCE[
            SOURCE.index("static HookInstallResult try_install_hook") : SOURCE.index(
                "static DWORD WINAPI install_hook"
            )
        ]
        restore = SOURCE[
            SOURCE.index("static void restore_every_hook") : SOURCE.index(
                'extern "C" __declspec(dllexport) void FF12CoopUnhook'
            )
        ]

        self.assertIn("compare_exchange_slot(", attempt)
        self.assertIn("g_original_present = reinterpret_cast<", attempt)
        self.assertNotIn("InterlockedExchangePointer", attempt)
        self.assertIn("compare_exchange_slot(", restore)
        self.assertIn(
            "Present unhook skipped because another hook owns the slot",
            restore,
        )
        self.assertNotIn("write_slot(", restore)

    def test_renderer_is_pinned_before_publishing_the_process_lifetime_hook(self):
        pin = SOURCE[
            SOURCE.index("static bool pin_renderer_module") : SOURCE.index(
                "static void release_partial_d3d_initialization"
            )
        ]
        worker = SOURCE[
            SOURCE.index("static DWORD WINAPI install_hook") : SOURCE.index(
                "static void restore_every_hook"
            )
        ]
        attempt = SOURCE[
            SOURCE.index("static HookInstallResult try_install_hook") : SOURCE.index(
                "static DWORD WINAPI install_hook"
            )
        ]
        self.assertIn("GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS", pin)
        self.assertIn("GET_MODULE_HANDLE_EX_FLAG_PIN", pin)
        self.assertLess(
            worker.index("pin_renderer_module()"),
            worker.index("try_install_hook("),
        )
        self.assertLess(
            attempt.index("compare_exchange_slot("),
            attempt.index("g_present_slot = present_slot"),
        )

    def test_hook_worker_retries_every_prepublication_failure_with_capped_backoff(self):
        worker = SOURCE[
            SOURCE.index("static DWORD WINAPI install_hook") : SOURCE.index(
                "static void restore_every_hook"
            )
        ]
        self.assertIn("while (!pin_renderer_module())", worker)
        self.assertIn("for (;;)", worker)
        self.assertIn("try_install_hook(++attempt_id)", worker)
        self.assertIn("Sleep(retry_delay)", worker)
        self.assertIn("next_hook_retry_delay(retry_delay)", worker)
        self.assertIn("kHookRetryMaximumMilliseconds", SOURCE)
        pin_loop = worker.index("while (!pin_renderer_module())")
        attempt_loop = worker.index("for (;;)", pin_loop)
        self.assertIn(
            "retry_delay = kHookRetryInitialMilliseconds;",
            worker[pin_loop:attempt_loop],
        )
        success = worker.index("result == HookInstallResult::installed")
        success_return = worker.index("return 0;", success)
        retry_sleep = worker.index("Sleep(retry_delay)", success_return)
        self.assertLess(success, success_return)
        self.assertLess(success_return, retry_sleep)
        for terminal_failure in ("return 1;", "return 2;", "return 3;"):
            self.assertNotIn(terminal_failure, worker)

    def test_each_hook_attempt_reclaims_window_class_and_partial_com_outputs(self):
        attempt = SOURCE[
            SOURCE.index("static HookInstallResult try_install_hook") : SOURCE.index(
                "static DWORD WINAPI install_hook"
            )
        ]
        self.assertEqual(attempt.count("return result;"), 1)
        cleanup = attempt[attempt.index("// D3D may return partial COM outputs") :]
        for resource in ("context", "device", "swapchain"):
            self.assertIn(f"release_ptr({resource})", cleanup)
        self.assertIn("if (window) DestroyWindow(window)", cleanup)
        self.assertIn(
            "if (registered_class) UnregisterClassW(class_name, instance)",
            cleanup,
        )
        self.assertNotIn("return HookInstallResult::", attempt)

    def test_successful_cas_stops_retry_before_another_hook_can_be_stacked(self):
        attempt = SOURCE[
            SOURCE.index("static HookInstallResult try_install_hook") : SOURCE.index(
                "static DWORD WINAPI install_hook"
            )
        ]
        worker = SOURCE[
            SOURCE.index("static DWORD WINAPI install_hook") : SOURCE.index(
                "static void restore_every_hook"
            )
        ]
        cas = attempt.index("compare_exchange_slot(")
        installed = attempt.index("result = HookInstallResult::installed", cas)
        self.assertLess(cas, installed)
        self.assertIn("return 0;", worker[worker.index("result =="):])

    def test_dllmain_rejects_the_load_when_its_only_worker_cannot_start(self):
        dllmain = SOURCE[SOURCE.index("BOOL APIENTRY DllMain") :]
        thread = dllmain.index("CreateThread(")
        failure = dllmain.index("if (!thread) return FALSE;", thread)
        closed = dllmain.index("CloseHandle(thread)", failure)
        self.assertLess(thread, failure)
        self.assertLess(failure, closed)

    def test_explicit_unhook_keeps_the_chained_present_alive_for_inflight_calls(self):
        restore = SOURCE[
            SOURCE.index("static void restore_every_hook") : SOURCE.index(
                'extern "C" __declspec(dllexport) void FF12CoopUnhook'
            )
        ]
        self.assertIn("g_present_slot = nullptr", restore)
        self.assertNotIn("g_original_present = nullptr", restore)
        self.assertIn("Present invocation can already", restore)

    def test_partial_device_initialization_is_fully_reclaimed_before_retry(self):
        cleanup = SOURCE[
            SOURCE.index("static void release_partial_d3d_initialization") :
            SOURCE.index("static const char* shader_source")
        ]
        for resource in (
            "g_panel_vs",
            "g_ring_vs",
            "g_copy",
            "g_hand_atlas",
            "g_constants",
            "g_context",
            "g_device",
        ):
            self.assertIn(f"release_ptr({resource})", cleanup)
        initialize = SOURCE[
            SOURCE.index("static bool initialize_for_swapchain") : SOURCE.index(
                "static void append_ring_vertex"
            )
        ]
        self.assertIn("g_device->GetDeviceRemovedReason()", initialize)
        self.assertGreaterEqual(
            initialize.count("release_partial_d3d_initialization()"),
            2,
        )

    def test_present_contains_independent_cpp_and_hardware_fault_boundaries(self):
        guard = SOURCE[
            SOURCE.index("static void render_color_pass_cxx_guard") : SOURCE.index(
                "static LRESULT CALLBACK dummy_window_proc"
            )
        ]
        self.assertIn("try {", guard)
        self.assertIn("catch (...) {", guard)
        self.assertIn("__try {", guard)
        self.assertIn("__except (EXCEPTION_EXECUTE_HANDLER)", guard)
        self.assertIn("render_color_pass_cxx_guard(swapchain)", guard)
        self.assertGreaterEqual(
            guard.count("InterlockedExchange(&g_render_faulted, 1)"),
            2,
        )

    def test_cave_discovery_never_dereferences_a_racing_virtual_region(self):
        discovery = SOURCE[
            SOURCE.index("static bool coop_cave_magic_matches") : SOURCE.index(
                "static bool valid_actor_for_handle"
            )
        ]
        self.assertIn("safe_read(address, observed, sizeof(observed))", discovery)
        self.assertIn("coop_cave_magic_matches(g_cave_base)", discovery)
        self.assertNotIn("memcmp(information.BaseAddress", discovery)

    def test_presentation_mailboxes_are_read_through_the_fault_safe_boundary(self):
        presentation = SOURCE[
            SOURCE.index("static bool p2_menu_rect") : SOURCE.index(
                "static bool is_game_swapchain"
            )
        ]
        self.assertIn("safe_read_value(cave + 0x208", presentation)
        self.assertIn("const bool badge_read =", presentation)
        self.assertIn("const bool feed_read =", presentation)
        self.assertIn("safe_read_value(cave + 0x1D08", presentation)
        self.assertNotIn("*(volatile uint32_t*)(cave", presentation)
        self.assertNotIn("memcpy(flat_rects, (void*)", presentation)

    def test_renderer_has_no_runtime_hotkey_toggle(self):
        self.assertNotIn("GetAsyncKeyState", SOURCE)
        self.assertNotIn("VK_F5", SOURCE)

    def test_hud_theme_is_resolved_by_published_member_slot(self):
        self.assertNotIn("party_slot_for_actor", SOURCE)
        self.assertIn("party_hud_visual_row", SOURCE)
        self.assertIn("790.0f + 60.0f * (float)visual_row", SOURCE)
        self.assertIn("const bool is_bottom = visual_row == 3", SOURCE)
        self.assertNotIn("slot == 2 ?", SOURCE)
        self.assertIn("cave, 0x26C, 0xE20, current_layout", SOURCE)
        self.assertIn("cave, 0x270, 0xB20, current_layout", SOURCE)
        self.assertIn(
            "cave, 0x821C, kP3ThemeColorOffset, current_layout",
            SOURCE,
        )

    def test_non_bottom_rows_include_their_complete_lower_edge(self):
        self.assertIn(
            "top + (is_bottom ? 56.0f : 60.0f)",
            SOURCE,
        )
        # A three-member party occupies visual rows 1..3. The first row starts
        # at logical y=850 and must end at the next row boundary y=910, not at
        # the old y=906 that produced a 2.67-pixel native strip at 720p.
        top = 790.0 + 60.0 * 1.0
        bottom = top + 60.0
        self.assertEqual((top, bottom), (850.0, 910.0))
        for scale in (1.0, 2.0 / 3.0, 4.0 / 3.0):
            with self.subTest(scale=scale):
                self.assertAlmostEqual(bottom * scale, 910.0 * scale)
                self.assertLessEqual(bottom * scale, (top + 60.0) * scale)

    def test_hud_rows_are_compacted_and_bottom_anchored_for_1_to_4(self):
        self.assertIn("party_hud_character_order", SOURCE)
        for first, second in ((0, 3), (3, 2), (2, 4), (4, 1), (1, 5)):
            self.assertIn(
                f"party_hud_character_order({first}) < "
                f"party_hud_character_order({second})",
                SOURCE,
            )
        self.assertIn("kHudLayoutSolo", SOURCE)
        self.assertIn("kHudLayoutTwo", SOURCE)
        self.assertIn("kHudLayoutThree", SOURCE)
        self.assertIn("kHudLayoutFour", SOURCE)
        self.assertIn("kHudLayoutGap", SOURCE)
        self.assertIn("kHudLayoutVaanBaschPenelo", SOURCE)
        self.assertIn("kHudLayoutStoryVariant", SOURCE)
        self.assertIn("party_hud_story_members_follow_mains", SOURCE)
        self.assertIn("party_hud_all_solo_mains_use_bottom_row", SOURCE)
        self.assertIn(
            "party_hud_three_member_permutations_keep_rows(0, 4, 5)",
            SOURCE,
        )
        self.assertIn(
            "party_hud_three_member_permutations_keep_rows(2, 1, 5)",
            SOURCE,
        )
        self.assertIn("party_hud_all_story_members_use_guest_row", SOURCE)
        for assertion in (
            "party_hud_visual_row(kHudLayoutSolo, 0) == 3",
            "party_hud_visual_row(kHudLayoutTwo, 0) == 3",
            "party_hud_visual_row(kHudLayoutTwo, 1) == 2",
            "party_hud_visual_row(kHudLayoutThree, 1) == 1",
            "party_hud_visual_row(kHudLayoutThree, 2) == 2",
            "party_hud_visual_row(kHudLayoutThree, 0) == 3",
            "party_hud_visual_row(kHudLayoutFour, 2) == 0",
            "party_hud_visual_row(kHudLayoutFour, 3) == 1",
            "party_hud_visual_row(kHudLayoutFour, 1) == 2",
            "party_hud_visual_row(kHudLayoutFour, 0) == 3",
            "party_hud_visual_row(kHudLayoutGap, 2) == 2",
            "party_hud_visual_row(kHudLayoutGap, 0) == 3",
            "party_hud_visual_row(kHudLayoutGap, 1) == -1",
            "party_hud_visual_row(kHudLayoutVaanBaschPenelo, 2) == 1",
            "party_hud_visual_row(kHudLayoutVaanBaschPenelo, 1) == 2",
            "party_hud_visual_row(kHudLayoutVaanBaschPenelo, 0) == 3",
            "party_hud_visual_row(kHudLayoutStoryVariant, 1) == 2",
            "party_hud_visual_row(kHudLayoutStoryVariant, 0) == 3",
        ):
            self.assertIn(assertion, SOURCE)

    def test_hud_layout_read_is_stable_and_fails_closed(self):
        self.assertIn("kBattleInfoGlobal = 0x02EBF190u", SOURCE)
        self.assertIn("kBattleCurrentLayoutOffset = 0x5A5Au", SOURCE)
        self.assertIn("kBattleUiLayoutOffset = 0x5A6Cu", SOURCE)
        self.assertIn("kEmptyPartyMember = 0xFFFFu", SOURCE)
        self.assertIn("read_current_party_layout(current_layout)", SOURCE)
        self.assertIn("memcmp(layout, second_current", SOURCE)
        self.assertIn("memcmp(ui_layout, second_ui", SOURCE)
        self.assertIn("coherent_party_layouts(layout, ui_layout)", SOURCE)
        self.assertIn("for (uint32_t current_index = 0;", SOURCE)
        self.assertIn("if (!found) return false;", SOURCE)

    def test_shader_uses_live_tints_without_neighbour_expansion(self):
        self.assertIn("float4 P1HudTint", SOURCE)
        self.assertIn("float4 P2HudTint", SOURCE)
        self.assertIn("float4 P3HudTint", SOURCE)
        self.assertIn("recolorPartyHud", SOURCE)
        self.assertIn("partyHudBlueMask", SOURCE)
        self.assertIn("themeSoftness", SOURCE)
        self.assertIn("tintChroma", SOURCE)
        self.assertIn("lerp(1.0, 0.72, themeSoftness)", SOURCE)
        self.assertIn("support += partyHudBlueMask(neighbour);", SOURCE)
        self.assertNotIn("p1HudOffsets", SOURCE)
        self.assertNotIn("const float2 hudOffsets[20]", SOURCE)

    def test_native_selection_ribbon_is_not_recolored_as_party_text(self):
        self.assertIn("float chromaticBlue", SOURCE)
        self.assertIn("smoothstep(0.130, 0.190, blueDominance)", SOURCE)
        self.assertIn("float luminousBlue", SOURCE)
        self.assertIn("smoothstep(0.560, 0.720, maximum)", SOURCE)
        self.assertIn("float authoredBlue = max(chromaticBlue, luminousBlue)", SOURCE)
        self.assertIn("return authoredBlue *", SOURCE)

    def test_reduced_native_hud_moves_colour_masks_with_the_same_anchor(self):
        self.assertIn("static bool party_hud_layout_reduced()", SOURCE)
        self.assertIn(
            "rect[0] == 122 && rect[1] == 242 &&",
            SOURCE,
        )
        self.assertIn(
            "rect[2] == 900 && rect[3] == 44 && gap == 42",
            SOURCE,
        )
        self.assertIn(
            "const float anchor_x = offset_x + 1972.0f * scale",
            SOURCE,
        )
        self.assertIn(
            "const float anchor_y = offset_y + 1027.0f * scale",
            SOURCE,
        )
        self.assertIn("const bool reduced_hud = party_hud_layout_reduced()", SOURCE)
        self.assertEqual(
            SOURCE.count("scale, offset_x, offset_y, reduced_hud,"),
            3,
        )

    def test_all_renderer_accents_follow_live_character_tints(self):
        self.assertIn("characterPanelTint(P2MenuTint)", SOURCE)
        self.assertIn("characterCursorTint(P1MenuTint)", SOURCE)
        self.assertIn("characterCursorTint(P2MenuTint)", SOURCE)
        self.assertIn("P1MenuTint.rgb / max(themeMaximum, 0.001)", SOURCE)
        self.assertIn("read_ring_theme(player, accent)", SOURCE)
        self.assertIn("kP1ThemeColorOffset = 0xE20", SOURCE)
        self.assertIn("kP2ThemeColorOffset = 0xB20", SOURCE)
        self.assertIn("kP3ThemeColorOffset = 0x8260", SOURCE)
        self.assertIn("kP1MenuThemeColorOffset = 0xE30", SOURCE)
        self.assertIn("kP2MenuThemeColorOffset = 0xB30", SOURCE)
        self.assertNotIn("color.rgb = lerp(color.rgb, gold, strength)", SOURCE)

    def test_optional_third_target_ring_has_an_independent_mailbox(self):
        self.assertIn("kRingP3HandleOffset = 0xDA4", SOURCE)
        self.assertIn("g_ring_animation[3]", SOURCE)
        self.assertIn("render_ring_pass(2", SOURCE)

    def test_constant_buffer_matches_sixteen_shader_registers(self):
        self.assertIn("buffer.ByteWidth = 256", SOURCE)
        self.assertIn("sizeof(ConstantData) == 256", SOURCE)

    def test_enemy_variant_textures_and_context_hooks_are_not_initialized(self):
        static_resources = SOURCE[
            SOURCE.index("static bool create_static_resources") : SOURCE.index(
                "static bool create_frame_resources"
            )
        ]
        swapchain_init = SOURCE[
            SOURCE.index("static bool initialize_for_swapchain") : SOURCE.index(
                "static void append_ring_vertex"
            )
        ]
        self.assertNotIn("load_enemy_variant_textures();", static_resources)
        self.assertNotIn(
            "install_texture_probe_context_hooks();",
            swapchain_init,
        )
        present = SOURCE[
            SOURCE.index(
                "static HRESULT STDMETHODCALLTYPE hooked_present"
            ) : SOURCE.index("static LRESULT CALLBACK dummy_window_proc")
        ]
        self.assertNotIn("selected_enemy", present)
        self.assertNotIn("enemy_variant", present)
        self.assertNotIn("texture_probe", present)
        self.assertNotIn("census_", present)
        self.assertNotIn("partition_", present)


if __name__ == "__main__":
    unittest.main()
