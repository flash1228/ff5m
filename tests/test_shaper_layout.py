"""Flow layout, resize and Designer reflection contracts for Shaper pages."""
# Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
# This file may be distributed under the terms of the GNU GPLv3 license.

import json
import pathlib
import sys
import unittest
from copy import deepcopy

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / '.py/klipper/plugins'))
from ff5m_ui.shaper import compare, page, progress
from ff5m_ui.shaper import history, start
from ff5m_ui.shaper.actions import ShaperAction
from ff5m_ui.shaper.components import SpectrumPlot
from ff5m_ui.shaper.results import ShaperResults
from ui import FeatherRenderer, Rect
from ui.reflection import reflect_page
from tests.feather_render_test_helper import RenderCapture
from tests.test_shaper_plot import raster_pixels


class ShaperLayoutTests(unittest.TestCase):
    def setUp(self):
        self.result = json.loads((ROOT/'tests/fixtures/shaper_reference.json').read_text())['cases'][0]
        self.renderer = FeatherRenderer()
        self.capture = RenderCapture(self.renderer)

    def test_flow_layout_resizes_canvas_and_preserves_touch_targets(self):
        widths = []
        for bounds in (Rect(0,56,720,386), Rect(0,56,800,386), Rect(0,56,1024,506)):
            with self.subTest(bounds=tuple(bounds)):
                view = ShaperResults(bounds)
                results = {'x': self.result}
                tree = view.write(results)
                self.renderer.send(self.renderer.begin_page(tree.title) + tree.draw(self.renderer, tree.state))
                canvas = tree.rect(page.ShaperRef.PLOT)
                widths.append(canvas.width)
                self.assertGreater(canvas.height, 100)
                for button in self.capture.latest.buttons.values():
                    area = button.bounds
                    self.assertGreaterEqual(area.width, 44)
                    self.assertGreaterEqual(area.height, 44)
                    self.assertGreaterEqual(area.x, bounds.x)
                    self.assertLessEqual(area.right, bounds.right)
                self.assertLess(canvas.bottom, tree.rect(page.ShaperRef.APPLY).y)
                view.select(ShaperAction('view', 'table'))
                table = view.write(results)
                self.renderer.send(self.renderer.begin_page(table.title) + table.draw(self.renderer, table.state))
                targets = [button.bounds for button in self.capture.latest.buttons.values()]
                for index, area in enumerate(targets):
                    self.assertGreaterEqual(area.width, 44)
                    self.assertGreaterEqual(area.height, 44)
                    self.assertGreaterEqual(area.x, bounds.x)
                    self.assertGreaterEqual(area.y, bounds.y)
                    self.assertLessEqual(area.right, bounds.right)
                    self.assertLessEqual(area.bottom, bounds.bottom)
                    for other in targets[index + 1:]:
                        self.assertFalse(area.x < other.right and other.x < area.right
                                         and area.y < other.bottom and other.y < area.bottom)
        self.assertEqual(widths, sorted(set(widths)))

    def test_all_pages_reflect_editable_components_and_chart_properties(self):
        for tree in (page.create_page(), compare.create_page(), deepcopy(progress.PAGE)):
            tree.draw(self.renderer)
            description = reflect_page(tree)
            self.assertEqual(description['page']['bounds'], list(tree.bounds))
            self.assertFalse(description['diagnostics'])
        chart = SpectrumPlot()
        self.assertTrue(chart.draw(self.renderer, {}, Rect(0,0,100,80)))

    def test_editing_the_chart_line_width_is_reflected_and_changes_the_rendered_stroke(self):
        view = ShaperResults()
        tree = view.write({'x': self.result})
        tree.draw(self.renderer, tree.state)
        before = raster_pixels(view.plot.commands)
        view.plot.line_width = 4
        tree.draw(self.renderer, tree.state)
        self.assertNotEqual(raster_pixels(view.plot.commands), before)
        pending = [reflect_page(tree)['tree']]
        while pending:
            node = pending.pop()
            if node['type'] == 'SpectrumPlot':
                self.assertEqual(node['properties']['line_width'], 4)
                break
            pending.extend(node['children'])
        else:
            self.fail('SpectrumPlot is missing from Designer reflection')

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
        for theme in ('DEFAULT', 'INDUSTRIAL'):
            for width in (720, 800, 1024):
                for band, previous_band, labels in cases:
                    result = dict(self.result)
                    result.pop('max_freq', None)
                    if band is not None:
                        result['max_freq'] = band
                    previous = None
                    if previous_band is not None:
                        previous = {'x': dict(result, max_freq=previous_band)}
                    with self.subTest(theme=theme, width=width, band=band, previous=previous_band):
                        self.renderer.set_theme(theme)
                        view = ShaperResults(Rect(0, 56, width, 386))
                        tree = view.write({'x': result}, reference=previous)
                        self.renderer.send(tree.draw(self.renderer, tree.state))
                        area = tree.rect(page.ShaperRef.X_TICKS)
                        ticks = [text for text in self.capture.latest.texts if area.y <= text.y < area.bottom]
                        self.assertEqual(tuple(text.value for text in ticks), labels)
                        for tick in ticks:
                            self.assertGreaterEqual(tick.x, area.x)
                            self.assertLessEqual(tick.x, area.right)
                            self.assertLessEqual(self.renderer.text_width(tick.value, tick.font), (area.width - 48) / 4)

    def test_five_history_rows_fit_the_screen_and_remain_editable(self):
        entries = [dict(id=str(i), saved_at=1793613222.-i*300, axes=['x', 'y']) for i in range(5)]
        for width in (720, 800, 1024):
            tree = deepcopy(history.PAGE)
            tree.bounds = Rect(0,56,width,386)
            self.renderer.send(tree.draw(self.renderer, {history.HistoryState.ENTRIES: entries}))
            self.assertEqual(sum(self.capture.latest.has_action('cal.shaper.record.%d'%i) for i in range(5)), 5)
            previous_bottom = tree.bounds.y
            for i in range(5):
                area = tree.rect('shaper.history.record.%d'%i)
                self.assertGreaterEqual(area.height,44)
                self.assertGreaterEqual(area.y,previous_bottom)
                self.assertLessEqual(area.bottom,tree.bounds.bottom)
                previous_bottom=area.bottom
            self.assertFalse(reflect_page(tree)['diagnostics'])

    def test_start_actions_fit_at_supported_widths_in_both_themes(self):
        for theme, width, sweep in (("DEFAULT", 800, True), ("INDUSTRIAL", 800, False),
                                    ("DEFAULT", 720, False), ("DEFAULT", 1024, True)):
            with self.subTest(theme=theme, width=width, sweep=sweep):
                self.renderer.set_theme(theme)
                tree = deepcopy(start.PAGE)
                tree.bounds = Rect(0, 56, width, 386)
                self.renderer.send(self.renderer.begin_page(tree.title) +
                                   tree.draw(self.renderer, {start.StartState.HAS_LAST: True,
                                                             start.StartState.SWEEP: sweep}))
                standard = self.capture.latest.button("cal.shaper.vibrations.standard")
                sweeping = self.capture.latest.button("cal.shaper.vibrations.sweeping")
                self.assertEqual((standard.state, sweeping.state),
                                 ("enabled", "selected") if sweep else ("selected", "enabled"))
                # Standard is the left choice, sweeping the right one, on one row.
                self.assertEqual(standard.bounds.y, sweeping.bounds.y)
                self.assertLessEqual(standard.bounds.right, sweeping.bounds.x)
                start_button = self.capture.latest.button("cal.shaper.start")
                instruction = self.capture.latest.text("Remove prints and objects from the bed.")
                self.assertLess(instruction.y, standard.bounds.y)
                # The method is chosen before the start action.
                self.assertLessEqual(standard.bounds.bottom, start_button.bounds.y)
                for button in (standard, sweeping):
                    self.assertGreaterEqual(button.bounds.height, 44)
                    self.assertLess(self.renderer.text_width(button.label, button.font), button.bounds.width)
                    self.assertLessEqual(button.bounds.right, tree.bounds.right)
                # The note under the choice fits within the width of that choice row.
                notes = [text for text in self.capture.latest.texts
                         if standard.bounds.bottom <= text.y < start_button.bounds.y]
                self.assertEqual(len(notes), 1)
                self.assertLessEqual(notes[0].x + self.renderer.text_width(notes[0].value, notes[0].font),
                                     sweeping.bounds.right)
                bottom = tree.bounds.y
                for action in ("cal.shaper.start", "cal.shaper.last"):
                    button = self.capture.latest.button(action)
                    area = button.bounds
                    self.assertGreaterEqual(area.height, 44)
                    self.assertLess(self.renderer.text_width(button.label, button.font), area.width)
                    self.assertGreaterEqual(area.y, bottom)
                    self.assertLessEqual(area.right, tree.bounds.right)
                    self.assertLessEqual(area.bottom, tree.bounds.bottom)
                    bottom = area.bottom
                self.assertFalse(reflect_page(tree)['diagnostics'])

    def test_comparison_exposes_only_available_method_actions(self):
        view = ShaperResults()
        data = {'x': self.result}
        view.write(data)
        view.select(ShaperAction('view','table'))
        tree = view.write(data)
        self.renderer.send(self.renderer.begin_page('Compare')+tree.draw(self.renderer,tree.state))
        self.assertFalse(self.capture.latest.has_action('cal.shaper.method.zvd'))
        for fit in self.result['shapers']:
            self.assertTrue(self.capture.latest.has_action(ShaperAction('method', fit['name']).wire_id))
        self.assertFalse(reflect_page(tree)['diagnostics'])

    def test_progress_stage_list_follows_requested_axes_and_cancel_state(self):
        for axes in (('x',),('y',),('x','y')):
            status=dict(job_id=1,state='processing',axis=axes[0],requested_axes=axes,completed_axes=(),
                        frequency=None,freq_start=5.,freq_end=100.,samples=32000,elapsed=40,cancel_requested=False)
            self.renderer.send(progress.render(self.renderer,status))
            self.assertTrue(self.capture.latest.has_action('cal.cancel'))
            status['cancel_requested'] = True
            self.renderer.send(progress.render(self.renderer,status))
            self.assertFalse(self.capture.latest.has_action('cal.cancel'))
