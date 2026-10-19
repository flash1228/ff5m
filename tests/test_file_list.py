## Observable file-list reuse, input and viewport contracts.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import unittest
from unittest import mock

from tests.test_feather_workflows import base_controller, FILES, FEATHER
from tests.feather_render_test_helper import RenderCapture
from ui import Rect


class FileListTest(unittest.TestCase):
    def browser(self, count):
        controller = base_controller()
        controller.page = FEATHER.ScreenPage.FILE_BROWSER
        controller.renderer = FEATHER.FeatherRenderer()
        capture = RenderCapture(controller.renderer)
        entries = [FILES.FileEntry("file-%d.gcode" % index, "/data/file-%d.gcode" % index)
                   for index in range(count)]
        controller.file_entry_cache["internal"] = entries
        controller._render_file_browser()
        return controller, capture, entries

    def test_paging_keeps_controls_and_selects_the_current_file(self):
        controller, capture, entries = self.browser(10)
        page = controller._file_list_tree
        first = page.node("files.slot.0")
        generation = controller.renderer.generation
        controller._handle_file_action("file.next")
        self.assertIs(controller._file_list_tree, page)
        self.assertIs(page.node("files.slot.0"), first)
        self.assertEqual(capture.latest.button("file.item0").label, entries[5].name)
        self.assertIsNone(controller.renderer.decode_action("%d:file.item0" % generation))
        controller._show_page = mock.Mock()
        controller._handle_file_action("file.item0")
        self.assertIs(controller.selected_file, entries[5])
        controller._show_page.assert_called_once_with(FEATHER.ScreenPage.FILE_CONFIRM)

    def test_short_and_empty_pages_remove_previous_row_input(self):
        controller, capture, entries = self.browser(7)
        controller._handle_file_action("file.next")
        self.assertEqual(capture.latest.button("file.item1").label, entries[6].name)
        self.assertFalse(capture.latest.has_action("file.item2"))
        self.assertFalse(capture.latest.has_action("file.item4"))
        controller.file_entry_cache["internal"] = []
        controller._render_file_browser()
        self.assertTrue(capture.latest.has_text("No G-code files"))
        self.assertFalse(any(action.startswith("file.item") for action in capture.latest.actions))
        self.assertEqual(controller.file_page, 0)

    def test_refresh_updates_labels_and_directory_actions_without_rebuilding(self):
        controller, capture, _entries = self.browser(5)
        page = controller._file_list_tree
        entry = FILES.FileEntry("USB", "/data/USB", directory=True)
        controller.file_entry_cache["internal"] = [entry]
        controller._render_file_browser()
        self.assertIs(controller._file_list_tree, page)
        self.assertEqual(capture.latest.button("file.item0").label, "USB  >")
        controller._render_file_browser = mock.Mock()
        controller._handle_file_action("file.item0")
        self.assertEqual(controller.file_source, "usb")
        self.assertIsNone(controller.selected_file)
        controller._render_file_browser.assert_called_once_with()

    def test_retained_rows_follow_viewport_and_theme_changes(self):
        controller, capture, _entries = self.browser(5)
        page = controller._file_list_tree
        original = page.node("files.slot.0")
        controller.renderer.set_theme("AMBER")
        for width, height in ((480, 480), (640, 400), (1024, 600), (800, 480)):
            controller.renderer.screen_width = width
            controller.renderer.screen_height = height
            controller._render_file_browser()
            self.assertIs(controller._file_list_tree, page)
            self.assertIs(page.node("files.slot.0"), original)
            for action, button in capture.latest.buttons.items():
                if action.startswith("file.item"):
                    area = button.bounds
                    self.assertTrue(page.bounds.contains(Rect(area.x, area.y, area.width, area.height)))
            self.assertEqual(capture.latest.button("file.item0").label, "file-0.gcode")


if __name__ == "__main__":
    unittest.main()
