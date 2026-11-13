"""Fullscreen chart navigation, data and input contracts."""
# Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
# This file may be distributed under the terms of the GNU GPLv3 license.

import copy
import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / '.py/klipper/plugins'))
from ff5m_ui.screen import ScreenPage
from ff5m_ui.shaper.actions import ShaperAction
from ff5m_ui.shaper.results import ShaperResults
from ff5m_ui.shaper.state import ShaperState
from ui import FeatherRenderer
from tests.test_feather_shaper import opened_result, pending_config
from tests.test_feather_workflows import base_controller, feature_on_screen


class ShaperFullscreenTests(unittest.TestCase):
    def setUp(self):
        cases = json.loads((ROOT / 'tests/fixtures/shaper_reference.json').read_text())['cases']
        self.results = {'x': copy.deepcopy(cases[1]), 'y': copy.deepcopy(cases[0])}

    def test_fullscreen_navigation_preserves_settings_without_applying_or_saving(self):
        host = base_controller()
        host.renderer = FeatherRenderer()
        native = opened_result(self.results)
        pending_config(host, native)
        feature, _ = feature_on_screen(host, 'calibration')
        feature.calibration_kind = 'shaper'
        host.page = ScreenPage.CALIBRATION_RESULT
        host._render_screen()
        host._show_message = mock.Mock()

        def tap(wire_id):
            self.assertIn(wire_id, host.renderer.shown_actions())
            action = feature.resolve_semantic_action(host.page, wire_id)
            self.assertIsNotNone(action)
            self.assertTrue(feature.handle_semantic_action(host.page, action))

        feature.handle_semantic_action(host.page, ShaperAction('method', 'ei'))
        tap('cal.shaper.scale')
        view = feature.shaper.view
        graph_plot = view.plot
        tap('cal.shaper.view.fullscreen')
        self.assertNotIn('cal.shaper.apply', host.renderer.shown_actions())
        self.assertEqual((view.axis, view.method, view.scale), ('x', 'ei', 'log'))
        self.assertFalse(graph_plot.commands)
        full_plot = view.plot
        self.assertTrue(full_plot.commands)
        tap('cal.shaper.scale')
        self.assertEqual(view.scale, 'linear')
        tap('cal.shaper.scale')
        self.assertEqual(view.scale, 'log')
        tap('cal.shaper.step.next')
        self.assertNotEqual(view.method, 'ei')
        tap('cal.shaper.step.prev')
        self.assertEqual(view.method, 'ei')
        tap('cal.shaper.axis.y')
        self.assertEqual((view.axis, view.method), ('y', self.results['y']['recommended']))
        self.assertEqual(view.page.state[ShaperState.AXIS], 'y')
        self.assertIn('nav.back', host.renderer.shown_actions())
        host.last_action_time = -100.
        host._dispatch_action('nav.back')
        self.assertEqual(view.mode, 'graph')
        self.assertEqual((view.axis, view.scale), ('y', 'log'))
        self.assertFalse(full_plot.commands)
        self.assertTrue(graph_plot.commands)
        self.assertEqual(host.page, ScreenPage.CALIBRATION_RESULT)
        self.assertEqual(host.gcode.commands, [])
        host._show_message.assert_not_called()

    def test_a_safety_header_action_restores_the_standard_header(self):
        host = base_controller()
        pending_config(host, opened_result(self.results))
        feature, capture = feature_on_screen(host, 'calibration')
        feature.calibration_kind = 'shaper'
        host.page = ScreenPage.CALIBRATION_RESULT
        host._render_screen()
        feature.handle_semantic_action(host.page, ShaperAction('view', 'fullscreen'))
        self.assertEqual(feature.shaper.view.mode, 'fullscreen')
        host.renderer.set_header_action('global.abort', 'ABORT', state='danger')
        host._render_screen()
        self.assertEqual(feature.shaper.view.mode, 'graph')
        self.assertIn('global.abort', host.renderer.shown_actions())

    def test_new_results_reset_fullscreen_and_release_its_raster(self):
        view = ShaperResults()
        view.write(self.results)
        view.select(ShaperAction('view', 'fullscreen'))
        tree = view.write(self.results)
        tree.draw(FeatherRenderer(), tree.state)
        plot = view.plot
        self.assertTrue(plot.commands)
        new_results = copy.deepcopy(self.results)
        self.assertIs(view.page_for(new_results), view.graph_page)
        self.assertIs(view.write(new_results), view.graph_page)
        self.assertFalse(plot.commands)
        self.assertEqual(view.mode, 'graph')
