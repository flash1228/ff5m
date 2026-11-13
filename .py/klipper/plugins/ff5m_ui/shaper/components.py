# Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
# This file may be distributed under the terms of the GNU GPLv3 license.

import math
from collections import OrderedDict

from ui import ThemeColor
from ui.bindings import resolve
from ui.components import Component
from ui.properties import EditorSpec, Invalidation, PropertySpec, SourceSpec, ValidationSpec, property_schema
from shaper_storage import MAX_SPECTRUM_FREQ, display_max_freq

from .plot import command_bytes, plot_commands
from .state import METHODS

CACHE_BUDGET_BYTES = 384 * 1024


def frequency_axis(*results):
    """Common X axis end: the widest recorded band, rounded up to a multiple
    of 40 Hz so the four grid intervals have whole 10 Hz labels. Bins up to
    200 Hz always exist, so the rounded axis never runs past the data."""
    band = max((display_max_freq(result) for result in results if result), default=MAX_SPECTRUM_FREQ)
    return 40 * math.ceil(band / 40.)


def visible_psd(result, max_freq):
    """PSD at the bins shown on an axis ending at max_freq."""
    return tuple(p for f, p in zip(result["freq_bins"], result["psd_sum"]) if f <= max_freq)


def spectrum_scale(psd, mode):
    """Common absolute calibration-PSD axis, independent of the selected fit.

    Saved values have Klipper's frequency normalization. They are not divided
    by their peak here, and no raw physical PSD units are invented for them.
    """
    peak = max(psd) or 1.
    if mode == "log":
        upper = math.ceil(math.log10(peak))
        lower = upper - 6
        return {"top": 1., "floor": 10. ** lower, "lower_log": lower,
                "ticks": tuple("1e%d" % exponent for exponent in (lower, upper - 3, upper)),
                "title": "CAL PSD (LOG)"}
    exponent = math.floor(math.log10(peak))
    unit = 10. ** exponent
    top = math.ceil(peak / unit * 2.) / 2. * unit
    return {"top": top, "ticks": tuple("%.2g" % (top * f / unit) for f in (0., .5, 1.)),
            "title": "CAL PSD x1e%d" % exponent}


def spectrum_commands(renderer, result, method, scale, bounds, before_color=ThemeColor.PRIMARY,
                      after_color=ThemeColor.SECONDARY, line_width=2, reference=None, max_freq=MAX_SPECTRUM_FREQ):
    selected = next(s for s in result["shapers"] if s["name"] == method)
    # Bins past the axis would be clamped onto its right edge.
    psd = visible_psd(result, max_freq)
    frequencies = result["freq_bins"][:len(psd)]
    # A transfer-function amplitude becomes power only after squaring.
    after = tuple(p * response * response for p, response in zip(psd, selected["response"]))
    curves = [(frequencies, psd, before_color), (frequencies, after, after_color)]
    measured = psd
    if reference:
        reference_psd = visible_psd(reference, max_freq)
        curves.insert(0, (reference["freq_bins"][:len(reference_psd)], reference_psd, ThemeColor.DIM))
        measured += reference_psd
    axis = spectrum_scale(measured, scale)
    if scale == "log":
        def transform(values):
            return tuple((math.log10(max(p, axis["floor"])) - axis["lower_log"]) / 6. for p in values)
        curves = [(xs, transform(ys), color) for xs, ys, color in curves]
    return plot_commands(renderer, bounds, tuple(curves), x_max=max_freq, y_max=axis["top"], line_width=line_width)


class SpectrumPlot(Component):
    """Only the spectrum raster is custom; surrounding labels belong to layout."""

    covers_bounds = True
    property_schema = property_schema(
        PropertySpec("result", (dict, type(None)), default=None, nullable=True,
                     editor=EditorSpec("collection_binding", group="Data"),
                     bindings=("direct", "derived"), source=SourceSpec(name="result", position=0)),
        PropertySpec("reference", (dict, type(None)), default=None, nullable=True,
                     editor=EditorSpec("collection_binding", group="Data"),
                     bindings=("direct", "derived"), source=SourceSpec(name="reference")),
        PropertySpec("method", str, default="mzv", bindings=("direct", "derived"),
                     editor=EditorSpec("select", choices=METHODS), validation=ValidationSpec(choices=METHODS),
                     source=SourceSpec(name="method", position=1)),
        PropertySpec("scale", str, default="linear", bindings=("direct", "derived"),
                     validation=ValidationSpec(choices=("linear", "log")),
                     source=SourceSpec(name="scale", position=2)),
        *(PropertySpec(name, (ThemeColor, str), default=default,
                       editor=EditorSpec("theme_color", group="Appearance", catalog="theme_tokens"),
                       bindings=("direct", "derived"), source=SourceSpec(name=name))
          for name, default in (("before_color", ThemeColor.PRIMARY), ("after_color", ThemeColor.SECONDARY))),
        PropertySpec("max_freq", int, default=200, bindings=("direct", "derived"),
                     editor=EditorSpec("number", group="Data"), validation=ValidationSpec(minimum=40, maximum=200),
                     source=SourceSpec(name="max_freq")),
        PropertySpec("line_width", int, default=2, editor=EditorSpec("number", group="Appearance"),
                     validation=ValidationSpec(minimum=1, maximum=6), bindings=("direct",),
                     invalidation=Invalidation.PAINT, source=SourceSpec(name="line_width")),
    )

    def __init__(self, result=None, method="mzv", scale="linear", before_color=ThemeColor.PRIMARY,
                 after_color=ThemeColor.SECONDARY, line_width=2, key=None, reference=None, max_freq=200):
        super().__init__(key=key)
        self.result, self.method, self.scale = result, method, scale
        self.reference, self.max_freq = reference, max_freq
        self.before_color, self.after_color, self.line_width = before_color, after_color, line_width
        self._cache_key, self._commands = None, ()
        self._bounds = None
        self._cache = OrderedDict()
        self._cache_bytes = 0

    @property
    def commands(self):
        return self._commands

    def clear_cache(self):
        self._cache.clear()
        self._cache_bytes = 0
        self._cache_key = None
        self._commands = ()
        self._bounds = None

    def state_signature(self, state):
        return (resolve(self.result, state), resolve(self.method, state), resolve(self.scale, state),
                resolve(self.before_color, state), resolve(self.after_color, state), resolve(self.line_width, state),
                resolve(self.reference, state), resolve(self.max_freq, state))

    def draw(self, renderer, state, bounds):
        before, after = resolve(self.before_color, state), resolve(self.after_color, state)
        signature = self.state_signature(state)
        result = signature[0] or {}
        reference = signature[6] or {}
        fit = next((s for s in result.get("shapers", ()) if s["name"] == signature[1]), {})
        data_key = (tuple(result.get("freq_bins", ())), tuple(result.get("psd_sum", ())), tuple(fit.get("response", ())),
                    tuple(reference.get("freq_bins", ())), tuple(reference.get("psd_sum", ())))
        key = (data_key, signature[1:6], signature[7], tuple(bounds), *(renderer.color(color) for color in
               (before, after, ThemeColor.DIM, ThemeColor.BACKGROUND, ThemeColor.BORDER)))
        if key != self._cache_key:
            cached = self._cache.pop(key, None)
            if cached is not None:
                self._commands, size = cached
                self._cache[key] = cached
            else:
                if result and fit:
                    self._commands = spectrum_commands(renderer, result, signature[1], signature[2],
                                                       bounds, before, after, signature[5], reference=reference,
                                                       max_freq=signature[7])
                else:
                    self._commands = plot_commands(renderer, bounds, ())
                size = sum(command_bytes(c) for c in self._commands)
                size += sum(len(part) * 8 for part in data_key) + 256
                while self._cache and self._cache_bytes + size > CACHE_BUDGET_BYTES:
                    _, (_, old_size) = self._cache.popitem(last=False)
                    self._cache_bytes -= old_size
                if size <= CACHE_BUDGET_BYTES:
                    self._cache[key] = (self._commands, size)
                    self._cache_bytes += size
            self._cache_key, self._bounds = key, bounds
        return self._commands
