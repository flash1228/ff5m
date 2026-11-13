## FF5M compact, theme-colored persistent status bar.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
## This file may be distributed under the terms of the GNU GPLv3 license

import math

from ui import FeatherRenderer, ThemeColor, ThemeRole
from ui.bindings import bind, state
from ui.identity import StateKey
from ui.lazy import LazyModule
from ui.renderer import FOOTER_HEIGHT

# The footer's tree is made with its first values: startup builds no
# declarative tree.
components = LazyModule("ui.components")
layout = LazyModule("ui.layout")


SEPARATOR = " · "
FONT = "JetBrainsMono 8pt"
METRICS_COLORS = (ThemeRole.TEMPERATURE_NOZZLE, ThemeRole.TEMPERATURE_BED, ThemeColor.TEXT,
                  ThemeColor.TEXT, ThemeColor.SUCCESS, ThemeColor.PRIMARY)
COLORFUL_COLORS = (ThemeColor.SECONDARY, ThemeColor.WARNING, ThemeColor.TEXT, ThemeColor.PRIMARY)


class StatusBarState(StateKey):
    """The status bar's fields: six (METRICS), four (COLORFUL) or the plain left and right texts."""

    __key_namespace__ = "ff5m.status_bar"
    METRICS = state(bool, default=False)
    COLORFUL = state(bool, default=False)
    PLAIN = state(bool, default=True)
    FIELD_0 = state(str, default="")
    FIELD_1 = state(str, default="")
    FIELD_2 = state(str, default="")
    FIELD_3 = state(str, default="")
    FIELD_4 = state(str, default="")
    FIELD_5 = state(str, default="")


FIELDS = (StatusBarState.FIELD_0, StatusBarState.FIELD_1, StatusBarState.FIELD_2,
          StatusBarState.FIELD_3, StatusBarState.FIELD_4, StatusBarState.FIELD_5)


def footer_values(extruder, bed, cpu_percent, memory_kib, network, status, style="METRICS"):
    if style == "VANILLA":
        return ("NOZZLE %.0f/%.0fC | BED %.0f/%.0fC" % (
            extruder["temperature"], extruder["target"], bed["temperature"], bed["target"]),
                "%s | %s" % (network, status.upper()))
    if style == "COLORFUL":
        return (SEPARATOR.join((
            "NOZZLE %.0f/%.0fC" % (extruder["temperature"], extruder["target"]),
            "BED %.0f/%.0fC" % (bed["temperature"], bed["target"]))),
                SEPARATOR.join((network, status.upper())))
    memory = "--"
    if memory_kib is not None and math.isfinite(memory_kib) and memory_kib >= 0:
        whole, fraction = divmod(memory_kib, 1024)
        # Display the nearest whole MiB, with an exact half rounded down.
        memory = str(int(whole) + (fraction > 512))
    cpu = "--" if cpu_percent is None else str(cpu_percent)
    left = SEPARATOR.join((
        "N %.0f/%.0fC" % (extruder["temperature"], extruder["target"]),
        "B %.0f/%.0fC" % (bed["temperature"], bed["target"]),
        "CPU %s%%" % cpu, "FREE %sMB" % memory))
    return left, SEPARATOR.join((network, status.upper()))


def _pair(first, colors, gap):
    """Two fields joined by a dim middle dot, each content-sized."""
    text = components.Text
    return layout.Row(
        text(bind(FIELDS[first]), colors[first], FONT, "left").width("content"),
        text("·", ThemeColor.DIM, FONT, "left").width("content"),
        text(bind(FIELDS[first + 1]), colors[first + 1], FONT, "left").width("content"),
        gap=gap).width("content")


class PrinterRenderer(FeatherRenderer):
    """Keep the shared footer lifecycle and specialize only FF5M's drawing."""

    def _footer_state(self, left, right):
        values = super()._footer_state(left, right)
        left_fields, right_fields = left.split(SEPARATOR), right.split(SEPARATOR)
        fields = left_fields + right_fields if len(left_fields) in (2, 4) and len(right_fields) == 2 else ()
        values[StatusBarState.METRICS] = len(fields) == 6
        values[StatusBarState.COLORFUL] = len(fields) == 4
        values[StatusBarState.PLAIN] = not fields
        for index, key in enumerate(FIELDS):
            values[key] = fields[index] if index < len(fields) else ""
        return values

    def _footer_content(self):
        from ui.footer import footer_panel, footer_texts
        # Columns are a dot's width apart; the dot is measured once, as the
        # runtime font metrics are the bundled ones.
        dot = self.text_width("·", FONT)
        # Content-sized temperatures and network/state pairs; METRICS centers
        # its CPU/memory pair in the column between them.
        row, when = layout.Row, layout.When
        metrics = row(
            _pair(0, METRICS_COLORS, 0), layout.Overlay(_pair(2, METRICS_COLORS, 0).align("center")).grow(1),
            _pair(4, METRICS_COLORS, 0), gap=dot)
        colorful = row(
            _pair(0, COLORFUL_COLORS, dot), layout.Spacer().grow(1), _pair(2, COLORFUL_COLORS, dot), gap=dot)
        return footer_panel(
            when(bind(StatusBarState.PLAIN), layout.Overlay(*footer_texts())),
            when(bind(StatusBarState.METRICS), metrics.offset(20, 2).size(760, FOOTER_HEIGHT)),
            when(bind(StatusBarState.COLORFUL), colorful.offset(20, 2).size(760, FOOTER_HEIGHT)))
