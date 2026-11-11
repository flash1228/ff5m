# Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
# This file may be distributed under the terms of the GNU GPLv3 license.

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / '.py/klipper/plugins'))
from shaper_storage import ResultHistory


class ShaperExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not all(importlib.util.find_spec(name) for name in ('numpy', 'matplotlib')):
            raise unittest.SkipTest('NumPy and Matplotlib are required for PNG export')
        spec = importlib.util.spec_from_file_location('shaper_export', ROOT / '.root/zshaper/calibrate_shaper.py')
        cls.export = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.export)
        cls.export.setup_matplotlib()

    def test_history_export_uses_recorded_band_and_keeps_every_curve_aligned(self):
        with tempfile.TemporaryDirectory() as directory:
            history = ResultHistory(directory)
            for metadata, maximum in (({}, 200.), ({'max_freq': 150.}, 150.), ({'max_freq': 135.}, 135.)):
                with self.subTest(metadata=metadata):
                    fixture = json.loads((ROOT / 'tests/fixtures/shaper_reference.json').read_text())['cases'][0]
                    result = dict(fixture, schema=1, kind='input_shaper', axis='x', **metadata)
                    entry = history.save({'x': result}, 1.)[0]
                    output, data, info = next(self.export.history_graphs(directory, entry['id'], 'x', directory))
                    fig = self.export.plot_freq_response(data)
                    try:
                        self.assertEqual(tuple(fig.axes[0].get_xlim()), (0., maximum))
                        for axis in fig.axes:
                            for line in axis.lines:
                                xs, ys = line.get_data()
                                self.assertEqual(len(xs), len(ys))
                                self.assertTrue(all(0. <= x <= maximum for x in xs))
                        self.assertGreater(len(fig.axes[0].lines[0].get_xdata()), 2)
                        fig.savefig(output, metadata=info)
                        self.assertTrue(Path(output).read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
                        self.assertEqual(history.load(entry['id'])[0]['x'], result)
                    finally:
                        self.export.matplotlib.pyplot.close(fig)

    def test_compensated_psd_uses_power_and_crops_every_series_at_the_same_frequency(self):
        result = json.loads((ROOT / 'tests/fixtures/shaper_reference.json').read_text())['cases'][0]
        result.update(max_freq=150., freq_bins=[0., 50., 100., 150., 200.],
                      psd_sum=[0., 8., 16., 4., 1000.], psd_x=[0., 8., 16., 4., 1000.],
                      psd_y=[0., 0., 0., 0., 0.], psd_z=[0., 0., 0., 0., 0.])
        for fit in result['shapers']:
            fit['response'] = [1., .5, 0., .25, 1.]
        with tempfile.TemporaryDirectory() as directory:
            history = ResultHistory(directory)
            entry = history.save({'x': dict(result, schema=1, kind='input_shaper', axis='x')}, 1.)[0]
            _, data, _ = next(self.export.history_graphs(directory, entry['id'], 'x', directory))
            fig = self.export.plot_freq_response(data)
            try:
                curves = {line.get_label(): line for line in fig.axes[0].lines}
                measured, compensated = curves['X+Y+Z'], curves['After\nshaper']
                self.assertEqual(list(measured.get_xdata()), [0., 50., 100., 150.])
                self.assertEqual(list(measured.get_ydata()), [0., 8., 16., 4.])
                self.assertEqual(list(compensated.get_xdata()), [0., 50., 100., 150.])
                # 8 * 0.5^2 = 2; 16 * 0^2 = 0; 4 * 0.25^2 = 0.25.
                self.assertEqual(list(compensated.get_ydata()), [0., 2., 0., .25])
                for line in fig.axes[1].lines:
                    if len(line.get_xdata()):
                        self.assertEqual(list(line.get_xdata()), [0., 50., 100., 150.])
                        self.assertEqual(list(line.get_ydata()), [1., .5, 0., .25])
            finally:
                self.export.matplotlib.pyplot.close(fig)
