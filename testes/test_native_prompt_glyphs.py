import unittest

from PIL import Image

import controller_bindings
import native_prompt_glyphs as glyphs


class AtlasBoxTests(unittest.TestCase):
    def test_atlases_are_present(self):
        self.assertTrue(glyphs.PAD_ATLAS_PATH.is_file(), glyphs.PAD_ATLAS_PATH)
        self.assertTrue(glyphs.KEY_ATLAS_PATH.is_file(), glyphs.KEY_ATLAS_PATH)

    def test_pad_uses_the_native_xbox_palette_not_the_gold_config_palette(self):
        self.assertEqual(glyphs.PAD_ATLAS_PATH.name, "buttonicon__pic00__pal15.png")
        a = glyphs.pad_glyph("A")
        self.assertIsNotNone(a)
        assert a is not None
        visible = [pixel for pixel in a.get_flattened_data() if pixel[3] >= 128]
        green = sum(
            1
            for red, green, blue, _alpha in visible
            if green > red * 1.25 and green > blue * 1.25
        )
        gold = sum(
            1
            for red, green, blue, _alpha in visible
            if red > 150 and green > 100 and blue < 80
        )
        self.assertGreater(green, gold * 2)

    def test_every_box_lies_inside_its_atlas(self):
        with (
            Image.open(glyphs.PAD_ATLAS_PATH) as pad,
            Image.open(glyphs.KEY_ATLAS_PATH) as keys,
        ):
            for name, box in glyphs.PAD_BUTTON_BOXES.items():
                self._assert_box(name, box, pad.width, pad.height)
            for name, box in glyphs.KEY_LABEL_BOXES.items():
                self._assert_box(name, box, keys.width, keys.height)
            for name, box in (
                ("plate_square", glyphs.PLATE_SQUARE_BOX),
                ("plate_wide", glyphs.PLATE_WIDE_BOX),
            ):
                self._assert_box(name, box, keys.width, keys.height)

    def _assert_box(self, name, box, width, height):
        left, top, right, bottom = box
        self.assertLess(left, right, name)
        self.assertLess(top, bottom, name)
        self.assertGreaterEqual(left, 0, name)
        self.assertGreaterEqual(top, 0, name)
        self.assertLessEqual(right, width, name)
        self.assertLessEqual(bottom, height, name)

    def test_no_box_is_blank(self):
        """A box that cuts empty atlas space publishes an invisible badge."""

        for name in glyphs.PAD_BUTTON_BOXES:
            cut = glyphs.pad_glyph(name)
            self.assertIsNotNone(cut, name)
            self.assertTrue(cut.getbbox(), f"{name} recortou area vazia")
        for name in glyphs.KEY_LABEL_BOXES:
            cut = glyphs.key_label_glyph(name)
            self.assertIsNotNone(cut, name)
            self.assertTrue(cut.getbbox(), f"{name} recortou area vazia")

    def test_pad_and_key_boxes_do_not_overlap_each_other(self):
        """Two controls sharing pixels means one of them is mapped wrong."""

        entries = list(glyphs.PAD_BUTTON_BOXES.items())
        for index, (name, box) in enumerate(entries):
            for other_name, other in entries[index + 1 :]:
                self.assertFalse(
                    box[0] < other[2]
                    and other[0] < box[2]
                    and box[1] < other[3]
                    and other[1] < box[3],
                    f"{name} e {other_name} se sobrepoem",
                )


class PromptBadgeTests(unittest.TestCase):
    def test_pad_badge_keeps_requested_height(self):
        badge = glyphs.prompt_badge("xinput", "A", 40)
        self.assertIsNotNone(badge)
        self.assertEqual(badge.height, 40)

    def test_keyboard_badge_keeps_requested_height(self):
        for label in ("SPC", "E", "ENT", "<"):
            badge = glyphs.prompt_badge("keyboard", label, 40)
            self.assertIsNotNone(badge, label)
            self.assertEqual(badge.height, 40, label)

    def test_spelled_key_is_wider_than_single_key(self):
        single = glyphs.prompt_badge("keyboard", "E", 40)
        spelled = glyphs.prompt_badge("keyboard", "SPC", 40)
        self.assertGreater(spelled.width, single.width)

    def test_unknown_binding_returns_none_so_caller_falls_back(self):
        self.assertIsNone(glyphs.prompt_badge("xinput", "NOT_A_BUTTON", 40))
        self.assertIsNone(glyphs.prompt_badge("keyboard", "NOT_A_KEY", 40))

    def test_aliases_resolve_to_a_real_label(self):
        for alias, name in glyphs.KEY_LABEL_ALIASES.items():
            self.assertIn(name, glyphs.KEY_LABEL_BOXES, alias)

    def test_same_label_differs_between_devices(self):
        """ "A" is a pad face button and a keyboard key; they must not match."""

        pad = glyphs.prompt_badge("xinput", "A", 40)
        key = glyphs.prompt_badge("keyboard", "A", 40)
        self.assertIsNotNone(pad)
        self.assertIsNotNone(key)
        self.assertNotEqual(pad.tobytes(), key.tobytes())


class PromptSourceTests(unittest.TestCase):
    def test_source_reports_the_device_that_supplied_the_binding(self):
        poller = controller_bindings.BindingPoller.__new__(
            controller_bindings.BindingPoller
        )
        poller.config = {
            "desktop": {"p1": False, "p2": True, "p3": False},
        }
        poller._active_device = {"p2": "xinput"}
        poller.bindings = lambda player, action: [{"device": "keyboard", "vk": 0x20}]
        poller.assigned_xinput = lambda player: None

        device, label = poller.action_prompt_source("p2", "confirm")
        self.assertEqual(device, "keyboard")
        self.assertEqual(label, "SPC")
        self.assertEqual(poller.action_prompt_label("p2", "confirm"), label)

    def test_pad_binding_keeps_its_device(self):
        poller = controller_bindings.BindingPoller.__new__(
            controller_bindings.BindingPoller
        )
        poller._active_device = {"p2": "xinput"}
        poller.bindings = lambda player, action: [{"device": "xinput", "control": "A"}]
        poller.assigned_xinput = lambda player: 0

        self.assertEqual(poller.action_prompt_source("p2", "confirm"), ("xinput", "A"))

    def test_pad_prompt_follows_the_native_decide_button(self):
        poller = controller_bindings.BindingPoller.__new__(
            controller_bindings.BindingPoller
        )
        poller._active_device = {"p2": "xinput"}
        poller.bindings = lambda player, action: [
            {"device": "xinput", "control": "A"}
        ]
        poller.assigned_xinput = lambda player: 0

        self.assertEqual(
            poller.action_prompt_source(
                "p2",
                "confirm",
                swap_face_buttons=True,
            ),
            ("xinput", "B"),
        )


if __name__ == "__main__":
    unittest.main()
