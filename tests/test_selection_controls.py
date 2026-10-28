## Indexed selection icons, press feedback and disabled-option contracts.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import io
from pathlib import Path
import shlex
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".py/klipper/plugins"))

from tests.feather_render_test_helper import RenderCapture
from feather.previews import decode_fxi1
from ff5m_ui import icons
from ff5m_ui.parameter_controls import ParameterAction
from ff5m_ui.selection_controls import OptionButton
from ui import FeatherRenderer, Rect, ThemeRole
from ui.renderer import BinaryCommand


class SelectionControlsTest(unittest.TestCase):
    def setUp(self):
        icons.load.cache_clear()
        self.addCleanup(icons.load.cache_clear)
        self.renderer = FeatherRenderer()
        self.capture = RenderCapture(self.renderer)
        self.bounds = Rect(25, 120, 750, 58)
        self.button = OptionButton(ParameterAction("option", 0), "BALANCED DETAIL", state="selected")

    def draw(self):
        commands = self.renderer.begin_page("Selection")
        commands += self.button.draw(self.renderer, {}, self.bounds)
        self.renderer.send(commands)
        return commands

    def image(self, commands):
        images = [command for command in commands if isinstance(command, BinaryCommand)]
        self.assertEqual(len(images), 1)
        return images[0]

    def test_selected_icon_is_native_one_bit_with_transparent_background(self):
        command = self.image(self.draw())
        image = decode_fxi1(command.payload)
        self.assertEqual((image["width"], image["height"], image["bpp"]), (24, 24, 1))
        self.assertEqual(image["palette"], (0, 0xff000000 | int(self.renderer.color(ThemeRole.BUTTON_SELECTED_TEXT), 16)))
        self.assertTrue(any(image["packed"]))
        self.assertEqual(image["packed"][:3], bytes(3))
        self.assertEqual(image["packed"][-3:], bytes(3))
        tokens = shlex.split(command)
        position = tokens.index("-p") + 1
        x, y = map(int, tokens[position:position + 2])
        self.assertTrue(self.bounds.contains(Rect(x, y, image["width"], image["height"])))
        caption = next(text for text in self.capture.latest.texts if text.value == "BALANCED DETAIL")
        self.assertGreaterEqual(caption.x - caption.max_width // 2, x + image["width"])
        self.assertTrue(self.renderer.send([command]))

    def test_press_and_restore_keep_the_caption_and_icon_in_the_same_surface(self):
        normal = self.image(self.draw()).payload
        self.assertTrue(self.renderer.flash_button("mod.option.0"))
        pressed = self.image(self.capture.batches[-1]).payload
        self.assertTrue(self.capture.latest.has_text("BALANCED DETAIL"))
        self.assertEqual(normal[28:], pressed[28:])
        self.assertNotEqual(normal[20:28], pressed[20:28])
        self.assertTrue(self.renderer.restore_button("mod.option.0"))
        self.assertEqual(self.image(self.capture.batches[-1]).payload, normal)
        self.assertTrue(self.capture.latest.has_text("BALANCED DETAIL"))

    def test_unselected_and_disabled_options_have_no_checkmark(self):
        for state in ("enabled", "disabled", "busy"):
            with self.subTest(state=state):
                self.button.state = state
                commands = self.draw()
                self.assertFalse(any(isinstance(command, BinaryCommand) for command in commands))
                self.assertEqual(self.capture.latest.has_action("mod.option.0"), state == "enabled")

        self.button.state = "selected"
        self.draw()
        self.button.state = "disabled"
        self.renderer.send(self.button.draw(self.renderer, {}, self.bounds))
        self.assertFalse(self.capture.latest.has_action("mod.option.0"))
        self.assertFalse(self.renderer.flash_button("mod.option.0"))

    def test_theme_changes_only_the_palette_and_resource_is_read_once(self):
        with mock.patch("builtins.open", wraps=open) as read:
            default = self.image(self.draw()).payload
            self.renderer.set_theme("AMBER")
            amber = self.image(self.draw()).payload
            self.draw()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(default[:20], amber[:20])
        self.assertEqual(default[28:], amber[28:])
        self.assertNotEqual(default[20:28], amber[20:28])

    def test_invalid_resource_is_not_cached_and_can_be_retried(self):
        with mock.patch("builtins.open", return_value=io.BytesIO(b"broken")):
            with self.assertRaisesRegex(ValueError, "Invalid FXI1 icon: controls/check"):
                self.draw()
        self.assertIsInstance(self.image(self.draw()), BinaryCommand)
