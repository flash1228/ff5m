# Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
# This file may be distributed under the terms of the GNU GPLv3 license.

import time

from ui import Rect

from .components import frequency_axis, spectrum_scale, visible_psd
from .page import CONTENT, ShaperRef, create_page
from .state import APPLICABLE_METHODS, ShaperState, comparison_values

# The shown chart or table; the values of ShaperAction("view", ...).
VIEWS = ("graph", "table", "fullscreen")


def _plot(page):
    return next(node for node in page.root.walk() if node.key == ShaperRef.PLOT)


class ShaperResults:
    """Presentation and graph cache; selected settings are supplied by Klipper."""

    def __init__(self, bounds=CONTENT):
        self.bounds = bounds
        self.results = None
        self.selections = {}
        self.mode = "graph"  # one of VIEWS
        self.table_page = None
        self.fullscreen_page = None
        self.axis = self.method = None
        self.scale = "linear"
        self.graph_page = create_page(bounds)

    @property
    def plot(self):
        """The spectrum plot shown: the fullscreen one, else the graph page's."""
        return _plot(self.page if self.mode == "fullscreen" else self.graph_page)

    def clear_cache(self):
        for page in (self.graph_page, self.fullscreen_page):
            if page is not None:
                _plot(page).clear_cache()

    def show_graph(self):
        self._show("graph")

    def _show(self, mode):
        # Switching views releases the rasters; the chart shown next draws its own.
        if self.mode != mode:
            self.clear_cache()
            self.mode = mode

    @property
    def page(self):
        if self.mode == "fullscreen":
            if self.fullscreen_page is None:
                from .fullscreen import BOUNDS, create_page
                # The whole screen at the content's width: header, content and the footer's strip.
                footer_height = BOUNDS.height - CONTENT.bottom
                self.fullscreen_page = create_page(Rect(0, 0, self.bounds.width, self.bounds.bottom + footer_height))
            return self.fullscreen_page
        if self.mode == "graph":
            return self.graph_page
        if self.table_page is None:
            from .compare import create_page
            self.table_page = create_page(self.bounds)
        return self.table_page

    def page_for(self, results):
        """The page shown once results are written: new results open on the graph."""
        return self.page if self.results is results else self.graph_page

    @property
    def can_save(self):
        return bool(self.selections)

    @property
    def can_apply(self):
        return self.method in APPLICABLE_METHODS

    @property
    def available_methods(self):
        return tuple(s["name"] for s in self.results[self.axis]["shapers"])

    def write(self, results, selections=None, reference=None, reference_at=None, saved_at=None):
        state = self.graph_page.state
        if self.results is not results:
            self.clear_cache()
            self.results, self.mode = results, "graph"
            self.table_page = None
            self.fullscreen_page = None
            self.selections = {}
            self.axis = next(axis for axis in ("x", "y") if axis in results)
            self.method, self.scale = results[self.axis]["recommended"], "linear"
        self.selections = dict(selections or {})
        result = results[self.axis]
        previous = (reference or {}).get(self.axis)
        reference_label = ""
        if reference_at:
            reference_label = "REF " + time.strftime("%Y-%m-%d %H:%M", time.localtime(reference_at))
            if not previous:
                reference_label += " / NO %s DATA" % self.axis.upper()
        if saved_at:
            reference_label += (" | " if reference_label else "") + "VIEW " + time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(saved_at))
        fit = next(s for s in result["shapers"] if s["name"] == self.method)
        # Comparing records share one frequency axis, as they share one PSD axis.
        max_freq = frequency_axis(result, previous)
        psd = visible_psd(result, max_freq) + (visible_psd(previous, max_freq) if previous else ())
        spectrum = spectrum_scale(psd, self.scale)
        state.update({
            ShaperState.AXIS: self.axis, ShaperState.METHOD: self.method, ShaperState.SCALE: self.scale,
            ShaperState.REFERENCE: {"freq_bins": previous["freq_bins"], "psd_sum": previous["psd_sum"]}
                                   if previous else {},
            ShaperState.REFERENCE_LABEL: reference_label,
            ShaperState.CAN_SAVE: self.can_save,
            ShaperState.SAVE_LABEL: "SAVE" + (" " + " & ".join(a.upper() for a in sorted(self.selections))
                                             if self.selections else ""),
            ShaperState.IS_SELECTED: self.selections.get(self.axis) == {
                "name": self.method, "freq": round(fit["freq"], 1)},
            ShaperState.RESULT: {"freq_bins": result["freq_bins"], "psd_sum": result["psd_sum"],
                                 "shapers": [{"name": self.method, "response": fit["response"]}]},
            ShaperState.TABLE: comparison_values(result), ShaperState.HAS_ZVD: "zvd" in self.available_methods,
            ShaperState.X_ENABLED: "x" in results, ShaperState.Y_ENABLED: "y" in results,
            ShaperState.METHOD_LABEL: "%s %.1fHz%s" % (self.method.upper(), fit["freq"],
                                                     " [REF]" if not self.can_apply else
                                                     (" *" if self.method == result["recommended"] else "")),
            ShaperState.CAN_APPLY: self.can_apply,
            ShaperState.METRICS: "VIB %.1f%% / SM %.3f" % (fit["vibrs"] * 100., fit["smoothing"]),
            ShaperState.ACCEL: "ACCEL ~%.0f mm/s2" % (round(fit["max_accel"] / 100.) * 100.),
            ShaperState.RECOMMENDED: "* RECOMMENDED / SCV %.1f" % result["scv"],
            ShaperState.FREQ_MAX: max_freq, ShaperState.SCALE_TITLE: spectrum["title"],
            ShaperState.TICK_LOW: spectrum["ticks"][0], ShaperState.TICK_MIDDLE: spectrum["ticks"][1],
            ShaperState.TICK_HIGH: spectrum["ticks"][2],
        })
        if self.mode != "graph":
            self.page.state.update(state)
        return self.page

    def select(self, action):
        if action.route == "axis" and action.value in self.results:
            self.axis, self.method = action.value, self.results[action.value]["recommended"]
        elif action.route == "method" and action.value in self.available_methods:
            self.method = action.value
        elif action.route == "step" and action.value in ("prev", "next"):
            methods = self.available_methods
            index = (methods.index(self.method) + (1 if action.value == "next" else -1)) % len(methods)
            self.method = methods[index]
        elif action.route == "scale":
            self.scale = "log" if self.scale == "linear" else "linear"
        elif action.route == "view" and action.value in VIEWS:
            self._show(action.value)
        else:
            return False
        return True
