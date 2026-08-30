import unittest
from dataclasses import replace

from PIL import ImageChops

from headless_world_target_overlay import (
    LiveTargetInfo,
    TargetHpAnimator,
    interpolate_projected_ring,
    is_probable_minimap_view,
    project_actor_point,
    project_point,
    projected_actor_ring,
    projected_ring_stroke_widths,
    render_projected_ring,
    render_target_info,
)


IDENTITY = (
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)

PROJECTION = (
    1.568078,
    0.0,
    0.0,
    0.0,
    0.0,
    2.787694,
    0.0,
    0.0,
    0.0,
    0.0,
    -1.001541,
    -1.0,
    0.0,
    0.0,
    -0.1001541,
    0.0,
)


class WorldTargetOverlayTests(unittest.TestCase):
    def test_target_hp_animates_damage_and_healing_without_changing_live_info(self):
        animator = TargetHpAnimator(duration=0.4)
        live = LiveTargetInfo("Basch", 0x1000, 4, (0.0, 0.0, 0.0), 50, 1000, 1000)

        self.assertEqual(animator.present(live, 10.0).current_hp, 1000)

        damaged = replace(live, current_hp=400)
        self.assertEqual(animator.present(damaged, 11.0).current_hp, 1000)
        self.assertEqual(animator.present(damaged, 11.2).current_hp, 700)
        self.assertEqual(animator.present(damaged, 11.4).current_hp, 400)
        # The actual snapshot remains authoritative and immutable.
        self.assertEqual(damaged.current_hp, 400)

        healed = replace(live, current_hp=900)
        self.assertEqual(animator.present(healed, 12.0).current_hp, 400)
        self.assertEqual(animator.present(healed, 12.2).current_hp, 650)
        self.assertEqual(animator.present(healed, 12.4).current_hp, 900)

    def test_target_hp_restarts_from_visible_value_and_snaps_on_retarget(self):
        animator = TargetHpAnimator(duration=0.4)
        first = LiveTargetInfo("First", 0x1000, 4, (0.0, 0.0, 0.0), 50, 1000, 1000)
        animator.present(first, 1.0)
        damaged = replace(first, current_hp=400)
        animator.present(damaged, 2.0)
        self.assertEqual(animator.present(damaged, 2.2).current_hp, 700)

        damaged_again = replace(first, current_hp=100)
        self.assertEqual(animator.present(damaged_again, 2.2).current_hp, 700)
        self.assertEqual(animator.present(damaged_again, 2.4).current_hp, 400)

        second = replace(
            first,
            name="Second",
            actor=0x2000,
            handle=5,
            current_hp=250,
            max_hp=500,
        )
        self.assertEqual(animator.present(second, 2.41).current_hp, 250)

    def test_center_point_projects_to_logical_client_center(self):
        projected = project_point((0.0, 0.0, -5.0), IDENTITY, PROJECTION)
        self.assertIsNotNone(projected)
        self.assertAlmostEqual(projected[0], 960.0)
        self.assertAlmostEqual(projected[1], 540.0)

    def test_actor_projection_converts_engine_handedness(self):
        direct = project_point((-1.0, -2.0, -5.0), IDENTITY, PROJECTION)
        actor = project_actor_point((1.0, 2.0, -5.0), IDENTITY, PROJECTION)
        self.assertEqual(actor, direct)

    def test_target_hud_contains_visible_pixels(self):
        info = LiveTargetInfo("Battery Mimic B", 1, 2, (0.0, 0.0, 0.0), 6, 100, 200)
        import headless_world_target_overlay as overlay

        image = render_target_info(info)
        # This target has no elemental weakness, so it gets the short card.
        self.assertEqual(
            image.size,
            (
                390,
                overlay.panel_height(
                    has_weak_row=False,
                    effect_rows=len(overlay.status_effect_lines(info.status_mask)),
                ),
            ),
        )
        self.assertIsNotNone(image.getchannel("A").getbbox())

        vaan = render_target_info(info, player=1)
        self.assertNotEqual(image.tobytes(), vaan.tobytes())
        self.assertGreater(
            sum(
                1
                for red, green, blue, alpha in vaan.get_flattened_data()
                if alpha and blue > red * 1.35 and blue > green * 1.15
            ),
            20,
        )

    def test_target_backdrop_keeps_a_dark_body_and_fades_to_clear(self):
        import headless_world_target_overlay as overlay

        image = overlay.render_target_info_backdrop(500, 100)
        self.assertEqual(image.size, (500, 100))
        alpha = image.getchannel("A")
        self.assertEqual(alpha.getpixel((0, 50)), overlay.TARGET_BACKDROP_PEAK_ALPHA)
        fade_start = (
            image.width
            - overlay.TARGET_BACKDROP_FADE_WIDTH
            - overlay.TARGET_BACKDROP_FADE_LEAD
        )
        self.assertEqual(
            alpha.getpixel((fade_start - 1, 50)),
            overlay.TARGET_BACKDROP_PEAK_ALPHA,
        )
        self.assertLess(
            alpha.getpixel((fade_start + 8, 50)),
            overlay.TARGET_BACKDROP_PEAK_ALPHA,
        )
        self.assertGreater(alpha.getpixel((430, 50)), alpha.getpixel((475, 50)))
        self.assertEqual(alpha.getpixel((499, 50)), 0)
        self.assertLess(alpha.getpixel((0, 99)), alpha.getpixel((0, 50)))

    def test_target_hud_health_bar_follows_character_theme(self):
        info = LiveTargetInfo("Battery Mimic B", 1, 2, (0.0, 0.0, 0.0), 6, 100, 200)
        basch = render_target_info(info, player=2, theme_name="fire")
        penelo = render_target_info(info, player=2, theme_name="gold")
        # The native gauge is a pale bar carrying only a hint of the identity
        # hue, so the fill is a lightened accent rather than the raw one.  The
        # contract under test is that it still follows the theme: same hue
        # ordering, brighter than the accent, and different per character.
        import menu_themes
        import headless_world_target_overlay as overlay

        # Sample the gauge where the layout actually puts it. A hand-copied
        # pixel silently started reading the panel background when the rows
        # moved, which is a layout change rather than a theming regression.
        rows = len(overlay.status_effect_lines(info.status_mask))
        left, top, _right, bottom = overlay.gauge_box(rows)
        sample = (left + 6, (top + bottom) // 2)

        for theme_name, rendered in (("fire", basch), ("gold", penelo)):
            accent = menu_themes.get_theme(theme_name).accent
            red, green, blue, alpha = rendered.getpixel(sample)
            self.assertEqual(alpha, 255)
            for channel, source in zip((red, green, blue), accent):
                self.assertGreaterEqual(channel, source)
            self.assertEqual(
                sorted(range(3), key=lambda i: (red, green, blue)[i]),
                sorted(range(3), key=lambda i: accent[i]),
            )
        self.assertNotEqual(basch.tobytes(), penelo.tobytes())

    def test_framed_panel_stays_inside_its_lane(self):
        """Nothing the card draws may reach the next player's lane.

        The image is wider than the lane, so anything past the pitch: slab,
        glow or the caption above it: is painted over by the next player and
        blinks in and out with their target.
        """

        import headless_world_target_overlay as overlay

        if overlay.TARGET_PANEL_STYLE != "framed":
            self.skipTest("only the framed layout paints its own background")

        info = LiveTargetInfo(
            "Dive Talon A",
            1,
            2,
            (0.0, 0.0, 0.0),
            28,
            4090,
            4090,
            flying=True,
            weak_mask=0x08,
        )
        panel = overlay.render_target_info(info, player=2)
        alpha = panel.getchannel("A")

        pitch = overlay.panel_pitch()
        slab_width = round(overlay.CARD_BOX_WIDTH * overlay.panel_scale())
        self.assertLessEqual(slab_width, pitch)
        if overlay.FRAMED_DRAW_SLAB:
            # Solid inside the slab, and completely clear past the lane.
            band = overlay.header_band()
            self.assertGreater(alpha.getpixel((slab_width // 2, band + 12)), 150)
        self.assertIsNone(alpha.crop((pitch, 0, panel.width, panel.height)).getbbox())

    def test_header_is_centred_between_rules_that_fade_at_the_tips(self):
        import headless_world_target_overlay as overlay

        if overlay.TARGET_PANEL_STYLE != "framed":
            self.skipTest("only the framed layout draws the caption band")

        band = overlay.header_sprite()
        text = overlay._header_sprite(
            overlay.FRAMED_HEADER_SCALE * overlay.panel_scale()
        )
        # The caption has to leave room for a rule on each side, which is why
        # it runs smaller than the classic one; without that it filled the slab.
        self.assertLess(text.width, band.width)
        margin = (band.width - text.width) // 2
        self.assertGreater(margin, 12, "no room left for the rules")

        alpha = band.getchannel("A")
        row = [alpha.getpixel((x, band.height // 2)) for x in range(band.width)]
        left = [value for value in row[:margin] if value]
        right = [value for value in row[band.width - margin :] if value]
        self.assertTrue(left and right, "the rules were not drawn")

        # Faint at the outer tip, solid where each rule meets the lettering.
        self.assertLess(left[0], 40)
        self.assertGreater(left[-1], 200)
        self.assertGreater(right[0], 200)
        self.assertLess(right[-1], 40)
        # Both rules are the same length, so the caption reads as centred.
        self.assertEqual(len(left), len(right))

    def test_libra_hides_the_numbers_but_never_the_afflictions(self):
        """No Libra, no numbers and no weakness — the afflictions still show.

        Libra buys the level, the HP and the weakness row. It does not buy the
        status icons: the game already draws those over the enemy itself, so
        hiding them on the card only disagreed with what is on screen.
        """

        import headless_world_target_overlay as overlay

        hidden = LiveTargetInfo(
            "Dive Talon A",
            1,
            2,
            (0.0, 0.0, 0.0),
            28,
            2000,
            4090,
            reveal_stats=False,
            flying=True,
            weak_mask=0x08,
            status_mask=(1 << 8) | (1 << 18),
        )
        shown = replace(hidden, reveal_stats=True)

        self.assertEqual(overlay.weak_element_names(hidden.weak_mask), ["earth"])

        # The affliction band is identical in both states. Only the weakness
        # row appears when Libra is active.
        effect_rows = len(overlay.status_effect_lines(hidden.status_mask))
        self.assertGreater(effect_rows, 0)
        self.assertEqual(
            overlay.render_target_info(hidden, player=2).height,
            overlay.panel_height(False, effect_rows),
        )
        self.assertGreater(
            overlay.render_target_info(shown, player=2).height,
            overlay.render_target_info(hidden, player=2).height,
        )

        # The gauge is the exception: it fills to the real ratio either way.
        # Each card is sampled at its own gauge, since the revealed one carries
        # the weakness row above it and sits lower.
        for info, rows in ((hidden, effect_rows), (shown, effect_rows)):
            left, top, right, bottom = overlay.gauge_box(rows)
            middle = (top + bottom) // 2
            edge = left + int((right - left) * info.current_hp / info.max_hp)
            card = overlay.render_target_info(info, player=2)
            inside = card.getpixel((edge - 4, middle))
            beyond = card.getpixel((edge + 6, middle))
            self.assertEqual(inside[3], 255, "the gauge shows no fill")
            self.assertNotEqual(inside, beyond, "the fill runs the whole bar")
        # The hidden card retains only the affliction band. The weakness row is
        # the sole row added by Libra.
        card = overlay.render_target_info(hidden, player=2)
        revealed = overlay.render_target_info(shown, player=2)
        self.assertLess(
            card.height,
            revealed.height,
            "the hidden card still reserves the weakness row",
        )
        self.assertEqual(
            card.height,
            overlay.panel_height(has_weak_row=False, effect_rows=effect_rows),
        )
        self.assertEqual(
            revealed.height,
            overlay.panel_height(has_weak_row=True, effect_rows=effect_rows),
        )

    def test_status_table_is_a_bijection_onto_the_atlas(self):
        """Every status with art owns exactly one sprite, and vice versa.

        The mapping was built by identifying the artwork, so the check that it
        is right is structural: FF12 has 32 statuses, the atlas holds 29 icons,
        and the table closes with no sprite used twice and none left over.  If
        an identification were wrong the counts would not meet.
        """

        import headless_world_target_overlay as overlay

        table = overlay.STATUS_EFFECTS
        self.assertEqual(len(table), 32)
        self.assertEqual([bit for bit, *_ in table], list(range(32)))

        # Anchor: Libra is index 30 in FF12's order and reads as bit 30 live.
        self.assertEqual(table[30][1], "Libra")
        self.assertEqual(
            [name for bit, name, _icon, _good in table if (1 << 30) & (1 << bit)],
            ["Libra"],
        )

        without_art = [name for _bit, name, icon, _good in table if icon is None]
        self.assertEqual(without_art, ["ko", "Petrify", "HP Critical", "X-Zone"])

        icons = [icon for _bit, _name, icon, _good in table if icon]
        self.assertEqual(len(icons), 28)
        self.assertEqual(len(set(icons)), 28, "a sprite is used by two statuses")

        folder = overlay.TRAIT_SPRITE_DIR / "status"
        available = {path.stem for path in folder.glob("*.png")}
        self.assertEqual(available - set(icons), set())
        self.assertEqual(set(icons) - available, set(), "table names a missing sprite")

    def test_libra_badge_hangs_from_the_captions_last_rule(self):
        """The badge is the viewer's own buff, pinned under the caption.

        It is drawn only when that player has Libra, and it lands centred on
        the last rule rather than at a corner someone measured by eye.
        """

        import headless_world_target_overlay as overlay

        if overlay.TARGET_PANEL_STYLE != "framed":
            self.skipTest("only the framed layout draws the caption band")

        base = LiveTargetInfo(
            "Wolf A", 1, 2, (0.0, 0.0, 0.0), 9, 155, 155, weak_mask=0x08
        )
        overlay.render_target_info.cache_clear()
        without = overlay.render_target_info(base, player=2)
        with_libra = overlay.render_target_info(
            replace(base, viewer_has_libra=True), player=2
        )
        self.assertNotEqual(without.tobytes(), with_libra.tobytes())

        # The badge is the only thing that changed, so the difference is the
        # badge, and it has to sit where the rule is.
        difference = ImageChops.difference(without, with_libra).getbbox()
        self.assertIsNotNone(difference)
        left, top, right, _bottom = difference
        centre = overlay.header_rule_end_center()
        self.assertLessEqual(left, centre)
        self.assertGreaterEqual(right, centre)
        # Immediately below the caption band, which is what "under the rule"
        # means once FRAMED_LIBRA_DROP is left at zero.
        self.assertEqual(
            top, overlay.header_band() + overlay._px(overlay.FRAMED_LIBRA_DROP)
        )
        # The rule it hangs from is right of the lettering, not over it.
        text = overlay._header_sprite(
            overlay.FRAMED_HEADER_SCALE * overlay.panel_scale()
        )
        slab = overlay._px(overlay.CARD_BOX_WIDTH)
        self.assertGreater(centre, (slab + text.width) // 2)
        # And nothing of it reaches the neighbouring player's lane.
        self.assertIsNone(
            with_libra.getchannel("A")
            .crop((overlay.panel_pitch(), 0, with_libra.width, with_libra.height))
            .getbbox()
        )

    def test_flying_gives_way_to_the_libra_badge(self):
        """Both want the top-right corner, so FLYING moves rather than collide."""

        import headless_world_target_overlay as overlay

        if overlay.TARGET_PANEL_STYLE != "framed":
            self.skipTest("only the framed layout shares the title row")
        if overlay.FRAMED_DRAW_SLAB:
            self.skipTest("the slab fills the gap this reads as clear space")

        flying_only = LiveTargetInfo(
            "Wolf A", 1, 2, (0.0, 0.0, 0.0), 9, 155, 155, flying=True
        )
        overlay.render_target_info.cache_clear()
        alone = overlay.render_target_info(flying_only, player=2)
        both = overlay.render_target_info(
            replace(flying_only, viewer_has_libra=True), player=2
        )
        self.assertNotEqual(alone.tobytes(), both.tobytes())

        badge = overlay.trait_sprite("status/libra")
        self.assertIsNotNone(badge)
        scale = overlay.status_icon_scale()
        badge_left = (
            overlay.header_rule_end_center() - max(1, int(badge.width * scale)) // 2
        )

        # The clear space the title gap is meant to leave really is clear: if
        # FLYING had kept its old right edge it would run under the badge.
        # Only the title row is sampled: the gauge below it spans the full
        # width, so a taller crop would read as occupied whatever the row does.
        gap = overlay._px(overlay.FRAMED_TITLE_GAP)
        top = overlay.header_band()
        band = both.getchannel("A").crop(
            (
                badge_left - gap,
                top,
                badge_left,
                top + overlay._px(overlay.FRAMED_HP_TOP),
            )
        )
        self.assertIsNone(band.getbbox(), "FLYING and the badge overlap")

        # The move is real: FLYING sits further left than when it is alone.
        title = (
            0,
            top,
            overlay._px(overlay.CARD_BOX_WIDTH),
            top + overlay._px(overlay.FRAMED_HP_TOP),
        )
        self.assertGreater(
            alone.getchannel("A").crop(title).getbbox()[2],
            both.getchannel("A").crop(title).getbbox()[2]
            - max(1, int(badge.width * scale)),
        )

    def test_anti_libra_has_a_distinct_visible_badge(self):
        import headless_world_target_overlay as overlay

        libra = overlay.trait_sprite("status/libra")
        anti_libra = overlay.trait_sprite("anti_libra")
        self.assertIsNotNone(libra)
        self.assertIsNotNone(anti_libra)
        assert libra is not None and anti_libra is not None
        self.assertEqual(libra.size, anti_libra.size)
        self.assertNotEqual(libra.tobytes(), anti_libra.tobytes())

        hidden = LiveTargetInfo(
            "Boss A",
            1,
            2,
            (0.0, 0.0, 0.0),
            50,
            9999,
            9999,
            reveal_stats=False,
        )
        marked = replace(hidden, anti_libra=True)
        overlay.render_target_info.cache_clear()
        self.assertNotEqual(
            overlay.render_target_info(hidden, player=2).tobytes(),
            overlay.render_target_info(marked, player=2).tobytes(),
        )

    def test_buffs_lead_the_status_run_and_afflictions_follow(self):
        """One unlabelled run: polarity is carried by the order alone."""

        import headless_world_target_overlay as overlay

        original = overlay.STATUS_REVIEW_EFFECTS
        try:
            overlay.STATUS_REVIEW_EFFECTS = ()
            # Poison is an affliction, Protect is not; the buff comes first
            # however the bits are ordered: Poison is bit 8, Protect bit 18.
            mask = (1 << 8) | (1 << 18)
            names = [
                name
                for bit, name, _icon, _good in overlay.STATUS_EFFECTS
                if mask & (1 << bit)
            ]
            self.assertEqual(sorted(names), ["Poison", "Protect"])
            self.assertEqual(overlay.status_icon_sequence(mask), ["protect", "poison"])
            self.assertEqual(
                overlay.status_icon_names(mask, beneficial=False), ["poison"]
            )
            self.assertEqual(
                overlay.status_icon_names(mask, beneficial=True), ["protect"]
            )
            # A polarity with nothing in it costs nothing: no caption, no gap.
            self.assertEqual(overlay.status_icon_sequence(1 << 8), ["poison"])
            self.assertEqual(overlay.status_icon_sequence(0), [])
            self.assertEqual(overlay.status_effect_lines(0), [])
        finally:
            overlay.STATUS_REVIEW_EFFECTS = original

    def test_a_long_status_run_wraps_instead_of_shrinking(self):
        """Every icon at once takes more than one line, and each line fits.

        The old layout kept a polarity on one row and shrank the icons until it
        did; the wrap keeps them at their drawn size and spends card height.
        """

        import headless_world_target_overlay as overlay

        if overlay.TARGET_PANEL_STYLE != "framed":
            self.skipTest("only the framed layout draws the status band")

        original = overlay.STATUS_REVIEW_EFFECTS
        try:
            overlay.STATUS_REVIEW_EFFECTS = ()
            every = sum(
                1 << bit for bit, _name, icon, _good in overlay.STATUS_EFFECTS if icon
            )
            lines = overlay.status_effect_lines(every)
            self.assertGreater(len(lines), 1, "28 icons cannot be one line")
            self.assertEqual(
                sum(len(line) for line in lines), 28, "an icon was dropped"
            )
            self.assertEqual(
                [name for line in lines for name in line],
                overlay.status_icon_sequence(every),
                "the wrap reordered the run",
            )

            # Measured the way the drawing measures it: each line has to fit
            # between the paddings, gaps included.
            gap = overlay._px(overlay.WEAK_ICON_GAP)
            for line in lines:
                width = sum(overlay._status_icon_width(name) for name in line) + gap * (
                    len(line) - 1
                )
                self.assertLessEqual(width, overlay.status_line_width())

            # And the card reserves room for the worst case it can produce.
            self.assertEqual(overlay.max_status_lines(), len(lines))
        finally:
            overlay.STATUS_REVIEW_EFFECTS = original

    def test_status_rows_never_reach_past_the_slab(self):
        """A long status list is wrapped, not spilled over the edge.

        Every buff and every affliction at once is far more than a line holds,
        so this exercises the path that has to give.
        """

        import headless_world_target_overlay as overlay

        if overlay.TARGET_PANEL_STYLE != "framed":
            self.skipTest("only the framed layout draws the status band")

        original = overlay.STATUS_REVIEW_EFFECTS
        try:
            overlay.STATUS_REVIEW_EFFECTS = tuple(
                name for _bit, name, icon, _good in overlay.STATUS_EFFECTS if icon
            )
            overlay.render_target_info.cache_clear()
            info = LiveTargetInfo(
                "Wolf A", 1, 2, (0.0, 0.0, 0.0), 9, 155, 155, weak_mask=0x08
            )
            panel = overlay.render_target_info(info, player=2)
            self.assertEqual(
                len(overlay.status_effect_lines(info.status_mask)),
                overlay.max_status_lines(),
            )
            # Nothing drawn may cross into the neighbouring player's lane.
            alpha = panel.getchannel("A")
            self.assertIsNone(
                alpha.crop(
                    (overlay.panel_pitch(), 0, panel.width, panel.height)
                ).getbbox()
            )
        finally:
            overlay.STATUS_REVIEW_EFFECTS = original
            overlay.render_target_info.cache_clear()

    def test_effect_band_shrinks_when_there_is_nothing_to_show(self):
        """The empty band is tighter than one holding a row, and the gauge
        follows the rows above it rather than the card's bottom edge."""

        import headless_world_target_overlay as overlay

        empty = overlay.panel_height(has_weak_row=False, effect_rows=0)
        one = overlay.panel_height(has_weak_row=False, effect_rows=1)
        two = overlay.panel_height(has_weak_row=False, effect_rows=2)
        self.assertLess(empty, one)
        self.assertLess(one, two)
        # The status lines are evenly pitched, so the second costs one row's
        # height while the first also has to pay back the collapsed band.
        self.assertEqual(two - one, overlay.gauge_box(2)[1] - overlay.gauge_box(1)[1])

        # The gauge slides down as rows appear above it.
        self.assertLess(overlay.gauge_box(0)[1], overlay.gauge_box(1)[1])
        self.assertLess(overlay.gauge_box(1)[1], overlay.gauge_box(2)[1])
        # And still ends inside the card.
        self.assertLess(overlay.gauge_box(2)[3], two)

    def test_framed_slab_lets_the_scene_through(self):
        """The slab is translucent, not a hole punched in the frame."""

        import headless_world_target_overlay as overlay

        if overlay.TARGET_PANEL_STYLE != "framed":
            self.skipTest("only the framed layout paints its own background")
        if not overlay.FRAMED_DRAW_SLAB:
            self.skipTest("the slab is switched off")

        info = LiveTargetInfo(
            "Dive Talon A",
            1,
            2,
            (0.0, 0.0, 0.0),
            28,
            4090,
            4090,
        )
        panel = overlay.render_target_info(info, player=2)
        # Sample the slab away from the border, the caption and every text row.
        rows = len(overlay.status_effect_lines(info.status_mask))
        _left, _top, right, bottom = overlay.gauge_box(rows)
        alpha = panel.getchannel("A").getpixel(
            (right - 4, (bottom + panel.height) // 2)
        )
        self.assertEqual(alpha, overlay.FRAMED_BODY_ALPHA)
        self.assertLess(alpha, 255, "the slab should not be fully opaque")

    def test_lane_pitch_follows_the_card_width(self):
        """Cards must stay evenly spaced whatever FRAMED_SCALE is set to.

        The lanes used to be a fixed 328 px apart, so shrinking the card left a
        gap about a third of a card wide between neighbours.
        """

        import headless_world_target_overlay as overlay

        original = overlay.FRAMED_SCALE
        try:
            # At full size the pitch has to match the number the lanes were
            # laid out with, or every anchor downstream shifts.
            overlay.FRAMED_SCALE = 1.0
            self.assertEqual(overlay.panel_pitch(), overlay.CARD_OWNED_WIDTH)

            for scale in (0.6, 0.75, 0.9):
                overlay.FRAMED_SCALE = scale
                slab = round(overlay.CARD_BOX_WIDTH * scale)
                gap = overlay.panel_pitch() - slab
                self.assertGreater(gap, 0, f"cards touch at scale {scale}")
                # The gap stays proportional instead of swallowing the card.
                self.assertLess(gap, slab // 4, f"gap too wide at scale {scale}")
        finally:
            overlay.FRAMED_SCALE = original

    def test_panel_style_is_reversible(self):
        """Switching the style back restores the header-and-divider layout."""

        import headless_world_target_overlay as overlay

        info = LiveTargetInfo(
            "Dive Talon A",
            1,
            2,
            (0.0, 0.0, 0.0),
            28,
            4090,
            4090,
            flying=True,
            weak_mask=0x08,
        )
        original = overlay.TARGET_PANEL_STYLE
        try:
            overlay.TARGET_PANEL_STYLE = "classic"
            overlay.render_target_info.cache_clear()
            classic = overlay.render_target_info(info, player=2)
            # The classic card draws no background, so its corner is clear and
            # its content reaches past the owned width.
            self.assertEqual(classic.getchannel("A").getpixel((4, 140)), 0)

            # The classic layout has no status band, so its height is the
            # legacy one whatever statuses the target carries.
            self.assertEqual(classic.height, overlay.panel_height(has_weak_row=True))
            rows = len(overlay.status_effect_lines(info.status_mask))

            overlay.TARGET_PANEL_STYLE = "framed"
            overlay.render_target_info.cache_clear()
            framed = overlay.render_target_info(info, player=2)
            self.assertNotEqual(classic.tobytes(), framed.tobytes())
            # The framed card is deliberately smaller, and whatever is laid out
            # below it reads its height from panel_height rather than a copy.
            self.assertEqual(
                framed.height,
                overlay.panel_height(has_weak_row=True, effect_rows=rows),
            )
            # Same content, and the framed card is the smaller of the two.
            # Compared without the status band, which only the framed layout
            # has and which can make it the taller one while under review.
            self.assertLess(
                overlay.panel_height(has_weak_row=True),
                classic.height,
            )
        finally:
            overlay.TARGET_PANEL_STYLE = original
            overlay.render_target_info.cache_clear()

    def test_actor_ring_uses_live_capsule_bounds(self):
        info = LiveTargetInfo("Vaan", 1, 2, (0.0, 0.0, -5.0), 6, 202, 202, 1.79, 0.5)
        points = projected_actor_ring(info, IDENTITY, PROJECTION)
        marker = render_projected_ring(points, now=0.2)
        self.assertIsNotNone(marker)
        self.assertGreater(marker.image.width, 100)
        self.assertIsNotNone(marker.image.getchannel("A").getbbox())

    def test_ring_interpolation_moves_and_morphs_halfway(self):
        source = ((0.0, 0.0), (10.0, 20.0))
        destination = ((100.0, 50.0), (30.0, 80.0))
        self.assertEqual(
            interpolate_projected_ring(source, destination, 0.5),
            ((50.0, 25.0), (20.0, 50.0)),
        )

    def test_projected_ring_stroke_scales_with_apparent_size(self):
        small = ((0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0))
        large = ((0.0, 0.0), (180.0, 0.0), (180.0, 120.0), (0.0, 120.0))
        small_widths = projected_ring_stroke_widths(small)
        large_widths = projected_ring_stroke_widths(large)
        self.assertTrue(all(a <= b for a, b in zip(small_widths, large_widths)))
        self.assertGreater(large_widths[1], small_widths[1])

    def test_minimap_view_signature_is_filtered(self):
        minimap = list(IDENTITY)
        minimap[6] = -0.94
        minimap[9] = -0.908
        minimap[10] = 0.33
        self.assertTrue(is_probable_minimap_view(minimap))

        scene = list(IDENTITY)
        scene[6] = -0.471
        scene[9] = 0.461
        scene[10] = -0.864
        self.assertFalse(is_probable_minimap_view(scene))


if __name__ == "__main__":
    unittest.main()
