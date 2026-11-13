## Validated Typer font metrics and the shared word-v1 layout algorithm.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

"""Validated Typer font metrics and the shared word-v1 layout algorithm."""

import json
import logging
import os
import re
import subprocess

from .engine import FontCatalog, FontMetricsBase, font_edit
from .font_policy import DEFAULT_FONT, FONT_FALLBACKS


SCHEMA = "font-metrics/v1"
WRAP_ALGORITHM = "word-v1"
FALLBACK_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             "font_metrics.json")
_WRAP_WORD = re.compile(r"[^ \t\r]+")

# Every edit of a FontMetric or FontMetrics attribute is noted in the engine's
# count; a FontMetrics rebuilds its engine catalog when the count moves, and the
# engine reads its classes' catalogs again, so measurement observes every edit.


class FontMetric:
    __slots__ = (
        "name", "advance_x", "monospaced", "advance_y", "top", "bottom",
        "ascent", "descent", "ascii_top", "ascii_bottom",
        "unicode_ranges", "fallback", "_catalog",
    )

    def __init__(self, name, advance_x, monospaced, advance_y, top, bottom,
                 unicode_ranges, fallback=None, ascent=None, descent=None,
                 ascii_top=None, ascii_bottom=None):
        self.name = name
        self.advance_x = advance_x
        self.monospaced = monospaced
        self.advance_y = advance_y
        self.top = top
        self.bottom = bottom
        # A manifest of an older Typer lacks these: its ink bounds stand in.
        self.ascent = -top if ascent is None else ascent
        self.descent = bottom if descent is None else descent
        self.ascii_top = top if ascii_top is None else ascii_top
        self.ascii_bottom = bottom if ascii_bottom is None else ascii_bottom
        self.unicode_ranges = unicode_ranges
        self.fallback = fallback

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        object.__setattr__(self, "_catalog", None)
        font_edit()

    @property
    def glyph_height(self):
        return self.bottom - self.top

    def _face(self):
        """The engine catalog of this face alone."""
        catalog = self._catalog
        if catalog is None:
            catalog = FontCatalog((self,), self.name)
            object.__setattr__(self, "_catalog", catalog)
        return catalog

    def character_advance(self, character):
        return self._face().advance(self.name, character)

    def text_advance(self, value):
        return self._face().advance(self.name, value)

    def supports_text(self, value):
        return self._face().supports(self.name, value)


class FontMetrics(FontMetricsBase):
    """The faces of a font manifest. The engine base measures on their catalog (font_catalog, normalize_font,
    normalize_for_text, metric, text_width, wrap_text, text_height, fitting_font)."""

    __slots__ = (
        "fonts", "names", "default_font", "revision", "_catalog",
        "_catalog_edits",
    )

    def __init__(self, fonts, default_font=None):
        self._catalog_edits = None
        self.fonts = dict((metric.name, metric) for metric in fonts)
        self.names = tuple(sorted(self.fonts))
        if not self.names:
            raise ValueError("font metrics must contain fonts")
        self.default_font = default_font or self.names[0]
        self.revision = 0
        if self.default_font not in self.fonts:
            raise ValueError("default_font must name a declared font")
        for metric in self.fonts.values():
            if metric.fallback is not None and metric.fallback not in self.fonts:
                raise ValueError(
                    "font fallback %r for %r is not declared" %
                    (metric.fallback, metric.name))

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        font_edit()

    @property
    def cache_key(self):
        return id(self), self.revision


def _integer(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % label)
    if positive and value <= 0:
        raise ValueError("%s must be positive" % label)
    return value


def parse_manifest(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("font manifest must be an object")
    if value.get("schema") != SCHEMA:
        raise ValueError("unsupported font manifest schema")
    if value.get("wrap_algorithm") != WRAP_ALGORITHM:
        raise ValueError("unsupported font wrap algorithm")
    entries = value.get("fonts")
    if not isinstance(entries, list) or not entries:
        raise ValueError("font manifest must contain fonts")

    metrics = []
    previous_name = None
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("font entry must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("font name must be non-empty")
        if previous_name is not None and name <= previous_name:
            raise ValueError("font entries must be uniquely sorted")
        previous_name = name
        monospaced = entry.get("monospaced")
        if not isinstance(monospaced, bool):
            raise ValueError("font monospaced must be boolean")
        ranges = entry.get("unicode_ranges")
        if not isinstance(ranges, list) or not ranges:
            raise ValueError("font unicode_ranges must be non-empty")
        normalized_ranges = []
        glyph_count = 0
        previous_end = -1
        for item in ranges:
            if not isinstance(item, list) or len(item) != 2:
                raise ValueError("font Unicode range must be a pair")
            start = _integer(item[0], "range start")
            end = _integer(item[1], "range end")
            if start < 0 or end < start or start <= previous_end or end > 0xffff:
                raise ValueError("font Unicode ranges must be sorted and disjoint")
            normalized_ranges.append((start, end))
            glyph_count += end - start + 1
            previous_end = end
        advance_x = entry.get("advance_x")
        if monospaced:
            advance_x = _integer(advance_x, "advance_x", positive=True)
        else:
            if not isinstance(advance_x, list) or len(advance_x) != glyph_count:
                raise ValueError("proportional advance_x must cover every glyph")
            advance_x = tuple(
                _integer(item, "advance_x", positive=True) for item in advance_x)
        advance_y = _integer(entry.get("advance_y"), "advance_y", positive=True)
        bounds = entry.get("glyph_bounds")
        if not isinstance(bounds, dict):
            raise ValueError("glyph_bounds must be an object")
        top = _integer(bounds.get("top"), "glyph top")
        bottom = _integer(bounds.get("bottom"), "glyph bottom")
        if bottom <= top:
            raise ValueError("glyph bounds must have positive height")
        metrics.append(FontMetric(
            name, advance_x, monospaced, advance_y, top, bottom,
            tuple(normalized_ranges), **_placement(entry)))
    return FontMetrics(metrics)


def _placement(entry):
    """The optional text placement metrics of a manifest entry."""
    if "ascent" not in entry:
        return {}
    ascii_bounds = entry.get("ascii_bounds")
    if not isinstance(ascii_bounds, dict):
        raise ValueError("ascii_bounds must be an object")
    return {
        "ascent": _integer(entry.get("ascent"), "ascent"),
        "descent": _integer(entry.get("descent"), "descent"),
        "ascii_top": _integer(ascii_bounds.get("top"), "ascii top"),
        "ascii_bottom": _integer(ascii_bounds.get("bottom"), "ascii bottom"),
    }


def apply_font_policy(metrics, default_font=None, fallbacks=None):
    """Apply project-owned selection and glyph fallback policy in place."""
    if default_font is None:
        default_font = DEFAULT_FONT
    if fallbacks is None:
        fallbacks = FONT_FALLBACKS
    if not isinstance(default_font, str) or not default_font:
        raise ValueError("default_font must be a non-empty string")
    if default_font not in metrics.fonts:
        raise ValueError("default_font must name a declared font")
    if not isinstance(fallbacks, dict):
        raise ValueError("font fallbacks must be a mapping")

    normalized_fallbacks = {}
    unknown = []
    for name, fallback in fallbacks.items():
        if (not isinstance(name, str) or not name or
                not isinstance(fallback, str) or not fallback):
            raise ValueError(
                "font fallback policy names must be non-empty strings")
        if name not in metrics.fonts or fallback not in metrics.fonts:
            unknown.append(name)
        else:
            normalized_fallbacks[name] = fallback
    if unknown:
        raise ValueError(
            "font fallback policy references undeclared fonts: %s" %
            ", ".join(sorted(unknown)))

    metrics.default_font = default_font
    for name in metrics.names:
        metrics.fonts[name].fallback = normalized_fallbacks.get(name)
    metrics.revision += 1
    return metrics


def parse_project_manifest(value):
    """Parse measured data and apply this project's overridable font policy."""
    return apply_font_policy(parse_manifest(value))


def load_fallback_metrics(path=FALLBACK_PATH):
    with open(path, "rb") as stream:
        return parse_project_manifest(stream.read())


def _inherit_policy(metrics, policy):
    if policy.default_font in metrics.fonts:
        metrics.default_font = policy.default_font
    for name in metrics.names:
        policy_metric = policy.fonts.get(name)
        metrics.fonts[name].fallback = (
            policy_metric.fallback if policy_metric is not None and
            policy_metric.fallback in metrics.fonts else None)
    metrics.revision += 1
    return metrics


def load_runtime_metrics(binary, timeout=0.35, fallback=None):
    fallback = fallback or load_fallback_metrics()
    try:
        completed = subprocess.run(
            [binary, "--font-manifest"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False)
        if completed.returncode != 0:
            raise ValueError("Typer returned %d" % completed.returncode)
        return _inherit_policy(parse_manifest(completed.stdout), fallback)
    except (OSError, TypeError, ValueError, UnicodeError,
            subprocess.TimeoutExpired) as error:
        logging.warning(
            "[feather_screen] unable to load Typer font manifest; using packaged fallback: %s",
            error)
        return fallback


_metrics = None


def get_font_metrics():
    global _metrics
    if _metrics is None:
        _metrics = load_fallback_metrics()
    return _metrics


def set_font_metrics(metrics):
    global _metrics
    _metrics = metrics
    font_edit()
    if metrics is not None:
        metrics.revision += 1
