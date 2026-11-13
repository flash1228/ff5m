"""Shaper pages: frequency ticks, history actions, cancellation state and touch targets."""
# Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import pathlib
import sys
import unittest
from copy import deepcopy

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / '.py/klipper/plugins'))
from ff5m_ui.shaper import page, progress, history, start
from ff5m_ui.shaper.actions import ShaperAction
from ff5m_ui.shaper.results import ShaperResults
from ui import FeatherRenderer
from tests.feather_render_test_helper import RenderCapture

# FEATHER_UI_VISUAL_LANGUAGE: the minimum interactive width and height on the 800x480 panel.
MIN_TOUCH = 44


class ShaperPageTests(unittest.TestCase):
    def setUp(self):
        self.result = json.loads((ROOT/'tests/fixtures/shaper_reference.json').read_text())['cases'][0]
        self.renderer = FeatherRenderer()
        self.capture = RenderCapture(self.renderer)

    def paint(self, tree, values=None):
        commands = self.renderer.begin_page(tree.title)
        commands += tree.draw(self.renderer, tree.state if values is None else values)
        self.renderer.send(commands)
        return self.capture.latest

    def test_frequency_ticks_are_whole_quarters_of_the_widest_compared_band(self):
        cases = (
            # FREQ_END 100 (the FF5M default) records 150 Hz; the axis rounds up to 160 Hz.
            (150., None, ('0', '40', '80', '120', '160 Hz')),
            (135., None, ('0', '40', '80', '120', '160 Hz')),
            (120., None, ('0', '30', '60', '90', '120 Hz')),
            (200., None, ('0', '50', '100', '150', '200 Hz')),
            (None, None, ('0', '50', '100', '150', '200 Hz')),
            (120., 150., ('0', '40', '80', '120', '160 Hz')),
            (150., 200., ('0', '50', '100', '150', '200 Hz')),
        )
        for band, previous_band, labels in cases:
            with self.subTest(band=band, previous=previous_band):
                result = dict(self.result)
                result.pop('max_freq', None)
                if band is not None:
                    result['max_freq'] = band
                previous = None
                if previous_band is not None:
                    previous = {'x': dict(result, max_freq=previous_band)}
                tree = ShaperResults().write({'x': result}, reference=previous)
                frame = self.paint(tree)
                area = tree.rect(page.ShaperRef.X_TICKS)
                ticks = sorted((text for text in frame.texts if area.y <= text.y < area.bottom),
                               key=lambda text: text.x)
                self.assertEqual(tuple(text.value for text in ticks), labels)

    def test_history_actions_follow_entries_when_the_list_changes(self):
        tree = deepcopy(history.PAGE)
        entries = [dict(id=str(i), saved_at=1793613222. - i * 300, axes=['x', 'y']) for i in range(5)]
        for count in (5, 2, 0, 1):
            with self.subTest(count=count):
                frame = self.paint(tree, {history.HistoryState.ENTRIES: entries[:count]})
                actions = {action for action in frame.actions if action.startswith('cal.shaper.record.')}
                self.assertEqual(actions, {'cal.shaper.record.%d' % i for i in range(count)})

    def test_progress_shows_only_requested_axis_stages_and_disables_cancel_when_requested(self):
        tree = deepcopy(progress.PAGE)
        cases = (
            (('x',), ('X SWEEP', 'X FIT')),
            (('y',), ('Y SWEEP', 'Y FIT')),
            (('x', 'y'), ('X SWEEP', 'X FIT', 'Y SWEEP', 'Y FIT')),
        )
        for axes, labels in cases:
            with self.subTest(axes=axes):
                status = dict(job_id=1, state='processing', axis=axes[0], requested_axes=axes,
                              completed_axes=(), frequency=None, freq_start=5., freq_end=100.,
                              samples=32000, elapsed=40, cancel_requested=False)
                frame = self.paint(tree, progress.values(status))
                stages = tuple(text.value for text in frame.texts
                               if text.value in ('X SWEEP', 'X FIT', 'Y SWEEP', 'Y FIT'))
                self.assertEqual(stages, labels)
                self.assertTrue(frame.has_action('cal.cancel'))
                status['cancel_requested'] = True
                frame = self.paint(tree, progress.values(status))
                self.assertFalse(frame.has_action('cal.cancel'))

    def test_every_shaper_control_is_a_separate_touch_target_inside_its_page(self):
        cases = json.loads((ROOT / 'tests/fixtures/shaper_reference.json').read_text())['cases']
        results = {'x': cases[1], 'y': cases[0]}
        entries = [dict(id=str(i), saved_at=1793613222. - i * 300, axes=['x', 'y']) for i in range(5)]
        status = dict(job_id=1, state='measuring', axis='x', requested_axes=('x', 'y'), completed_axes=(),
                      frequency=43., freq_start=5., freq_end=100., samples=32000, elapsed=40,
                      cancel_requested=False)
        shown = [('start', deepcopy(start.PAGE), start.values(entries[0], True)),
                 ('history', deepcopy(history.PAGE), {history.HistoryState.ENTRIES: entries}),
                 ('progress', deepcopy(progress.PAGE), progress.values(status))]
        for view_name in ('graph', 'table', 'fullscreen'):
            view = ShaperResults()
            view.write(results)
            view.select(ShaperAction('view', view_name))
            tree = view.write(results)
            shown.append((view_name, tree, tree.state))
        for name, tree, values in shown:
            with self.subTest(page=name):
                renderer = FeatherRenderer()
                capture = RenderCapture(renderer)
                renderer.send(tree.draw(renderer, values))
                targets = [button.bounds for button in capture.latest.buttons.values()]
                self.assertTrue(targets)
                bounds = tree.bounds
                for index, area in enumerate(targets):
                    self.assertGreaterEqual(min(area.width, area.height), MIN_TOUCH, area)
                    self.assertTrue(bounds.x <= area.x and area.right <= bounds.right
                                    and bounds.y <= area.y and area.bottom <= bounds.bottom, area)
                    for other in targets[index + 1:]:
                        self.assertFalse(area.x < other.right and other.x < area.right
                                         and area.y < other.bottom and other.y < area.bottom, (area, other))
