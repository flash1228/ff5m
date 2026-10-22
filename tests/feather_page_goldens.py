## Recorded page output used as the Feather framework regression oracle
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

"""Record what declarative pages send to Typer across state scenarios.

Each scenario drives a real page through public page and renderer calls and
records, per step, the commands, the prepared hitboxes and the geometry that
changed. A press step commits the page as the screen does and records the
action each input region dispatches and the feedback each button sends. The
text is compared with stored goldens, so a framework change that alters any
output of these pages becomes visible in review. The native node engine must
reproduce the same text byte for byte.
"""

import copy
import importlib
import json
import pathlib
import re
import sys
from enum import Enum
from types import SimpleNamespace

PLUGINS = pathlib.Path(__file__).parents[1] / ".py" / "klipper" / "plugins"
sys.path.insert(0, str(PLUGINS))

from feather.screen.pages.exclude_objects import exclude_values  # noqa: E402
from feather.settings import mod  # noqa: E402
from ff5m_ui import file_list, parameter_controls  # noqa: E402
from ff5m_ui.benchmark import layout_page  # noqa: E402
from ff5m_ui.filament import runtime as filament_runtime  # noqa: E402
from ff5m_ui.heat import page as heat_page  # noqa: E402
from ff5m_ui.move.actions import JOYSTICK_XY, JOYSTICK_Z  # noqa: E402
from ff5m_ui.move.state import MoveState  # noqa: E402
from ff5m_ui.z_offset.actions import ZONE_ACTIONS  # noqa: E402
from ff5m_ui.z_offset.paper.state import PaperState  # noqa: E402
from ff5m_ui.z_offset.summary.state import SummaryState  # noqa: E402
from feather.screen.pagination import Pagination  # noqa: E402
from ui import FeatherRenderer  # noqa: E402
from ui.actions import action_metadata  # noqa: E402
from ui.bindings import state_spec  # noqa: E402
from ui.identity import StateKey  # noqa: E402
from ui.reflection import reflect_page  # noqa: E402
from tests.feather_render_test_helper import RenderCapture  # noqa: E402
from tests.test_feather_preview_settings import PreviewSettingsTest  # noqa: E402

MODULE_PAGES = (
    "ff5m_ui.benchmark.layout_page", "ff5m_ui.benchmark.page",
    "ff5m_ui.calibration_screws.page", "ff5m_ui.exclude_model",
    "ff5m_ui.filament.action.page", "ff5m_ui.filament.material.page",
    "ff5m_ui.heat.page", "ff5m_ui.home.page", "ff5m_ui.model_preview.page",
    "ff5m_ui.model_preview.print_page", "ff5m_ui.move.joystick.page",
    "ff5m_ui.move.step.page", "ff5m_ui.printing.more", "ff5m_ui.printing.page",
    "ff5m_ui.z_offset.paper.page", "ff5m_ui.z_offset.paper_briefing.page",
    "ff5m_ui.z_offset.safe.page", "ff5m_ui.z_offset.safe_briefing.page",
    "ff5m_ui.z_offset.summary.page",
)

LONG_TEXT = "Golden text long enough to wrap or truncate in narrow areas 123"

# Structured state needs realistic shapes; scalar keys get generic variants.
STRUCTURED_VARIANTS = {
    MoveState.CURSOR: ((JOYSTICK_XY.wire_id, 300, 180), (JOYSTICK_Z.wire_id, 546, 200)),
    PaperState.GAUGE: ({"value": 1.5, "minimum": 0.0, "maximum": 4.0, "initial": 0.5},),
    SummaryState.RESULTS: (dict(zip(ZONE_ACTIONS, (-0.124, -0.125, 0.123, -0.121, -0.122))),),
}

EXCLUDE_OBJECTS = tuple(
    {"name": "part_%d" % index,
     "polygon": [[10 + 40 * index, 10], [40 + 40 * index, 10],
                 [40 + 40 * index, 60], [10 + 40 * index, 60]]}
    for index in range(7)) + ({"name": "no_geometry_part", "polygon": None},)


def _variants(key, default):
    if key in STRUCTURED_VARIANTS:
        return STRUCTURED_VARIANTS[key]
    spec = state_spec(key)
    if spec.choices is not None:
        return tuple(value for value in spec.choices if value != default)
    kind = spec.value_type
    if kind is bool:
        return (not default,)
    if kind is str:
        return (LONG_TEXT, "") if default else (LONG_TEXT,)
    if kind in (int, float):
        base = default or 0
        if spec.maximum is not None:
            values = (spec.maximum, spec.minimum)
        elif spec.minimum is not None:
            values = (base + kind(37.25 if kind is float else 37), spec.minimum)
        else:
            values = (base + kind(37.25 if kind is float else 37), -base - kind(5))
        return tuple(kind(value) for value in values if value is not None and value != default)
    return ()


# Schemas, source provenance and canvas capabilities are static contracts
# with absolute paths; the goldens keep the values a node publishes.
REFLECTED_PAGE_FIELDS = ("state", "dependencies", "actions", "bindings")
REFLECTED_NODE_FIELDS = (
    "id", "type", "ref", "bounds", "visible", "own_visible", "container",
    "layout", "layout_origins", "properties", "property_origins", "bindings",
    "actions", "condition", "style", "list_view", "component_template_instance",
    "template_instance", "structure_slot")


def _portable(value):
    """Drop source locations and object addresses, which vary per checkout and run."""
    if isinstance(value, dict):
        return dict((key, _portable(item)) for key, item in value.items() if key != "location")
    if isinstance(value, (list, tuple)):
        return [_portable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return re.sub(r" at 0x[0-9a-f]+", "", str(value))


def _json(value):
    return json.dumps(_portable(value), sort_keys=True)


def _name(value):
    if isinstance(value, StateKey):
        return "%s.%s" % (type(value).__name__, value.name)
    if isinstance(value, Enum):
        value = value.value
    return str(value)


class PageRecorder:
    """Drive one page and record what each step sends."""

    def __init__(self, factory):
        self.factory = factory
        self.lines = []
        self._reset()

    def _reset(self):
        self.page = self.factory()
        self.renderer = FeatherRenderer()
        self.geometry = {}

    def _geometry(self):
        rects = {}

        def visit(node, path):
            label = "%s %s" % (path, type(node).__name__)
            if node.key is not None:
                label += " " + _name(node.key)
            try:
                rects[label] = tuple(self.page.layout.rect(node))
            except KeyError:
                pass  # Hidden branches are not arranged.
            for index, child in enumerate(node.render_children()):
                visit(child, "%s.%d" % (path, index))

        visit(self.page.root, "r")
        return rects

    def step(self, name, action):
        self.lines.append("## " + name)
        try:
            commands = action()
        except Exception as error:
            self.lines.append("error %s: %s" % (type(error).__name__, error))
            self._reset()
            self.page.draw(self.renderer, {})
            self.geometry = self._geometry()
            return
        self.lines.extend("cmd " + str(command) for command in commands)
        self.lines.extend(
            "hit %s %d %d %d %d %s %s" % (
                hit.action, hit.x, hit.y, hit.width, hit.height,
                hit.continuous, hit.layer)
            for hit in self.renderer.hitboxes)
        geometry = self._geometry()
        for label, rect in sorted(geometry.items()):
            if self.geometry.get(label) != rect:
                self.lines.append("rect %s %d %d %d %d" % ((label,) + rect))
        for label in sorted(set(self.geometry) - set(geometry)):
            self.lines.append("gone " + label)
        self.geometry = geometry

    def draw(self, name, state=None):
        self.step(name, lambda: self.page.draw(self.renderer, state if state is not None else {}))

    def reflect(self, name):
        """Record what Designer inspects: values, bindings, actions, origins."""
        self.lines.append("## " + name)
        data = reflect_page(self.page)
        for field in REFLECTED_PAGE_FIELDS:
            self.lines.append("page %s %s" % (field, _json(data.get(field))))

        def visit(node):
            self.lines.append("node " + _json(dict(
                (field, node.get(field)) for field in REFLECTED_NODE_FIELDS)))
            for child in node["children"]:
                visit(child)

        visit(data["tree"])

    def update(self, name, state):
        self.step(name, lambda: self.page.update(self.renderer, state))

    def each_key(self):
        """Change every state key alone, then restore its default."""
        for key in self.page.state_schema:
            default = state_spec(key).default_value()
            for value in _variants(key, default):
                label = "%s=%r" % (_name(key), value)
                self.update("set " + label, {key: value})
                self.update("restore " + _name(key), {key: default})

    def press(self, draw=lambda page, renderer: page.draw(renderer, {})):
        """Commit a fresh page like the screen, then press each button."""
        self.lines.append("## press")
        page, renderer = self.factory(), FeatherRenderer()
        capture = RenderCapture(renderer)
        with renderer.compose():
            renderer.send(draw(page, renderer))
        for wire in sorted(set(hit.action for hit in renderer.hitboxes)):
            self.lines.append("action %s %s" % (wire, _json(action_metadata(page.resolve_action(wire)))))
        for wire in sorted(renderer._buttons):
            del capture.batches[:]
            renderer.flash_button(wire)
            renderer.restore_button(wire)
            self.lines.append("press " + wire)
            self.lines.extend("cmd " + str(command) for batch in capture.batches for command in batch)

    def all_keys(self):
        values = {}
        for key in self.page.state_schema:
            variants = _variants(key, state_spec(key).default_value())
            if variants:
                values[key] = variants[0]
        self.update("set all", values)
        self.draw("redraw all", values)


def _module_page(name):
    module = importlib.import_module(name)
    return lambda: copy.deepcopy(module.PAGE)


def _standard(factory, extra=()):
    recorder = PageRecorder(factory)
    recorder.draw("draw defaults")
    recorder.reflect("reflect defaults")
    recorder.each_key()
    recorder.all_keys()
    for name, action in extra:
        action(recorder)
    recorder.press()
    return recorder.lines


def _exclude(recorder):
    for label, selected, excluded, page in (
            ("objects", set(), set(), 0),
            ("selection", {"part_1", "part_2"}, {"part_2"}, 0),
            ("second page", {"part_6"}, set(), 1)):
        recorder.update("exclude " + label, exclude_values(
            [dict(item) for item in EXCLUDE_OBJECTS], excluded,
            (0, 0, 300, 300), selected, page))


def _theme(recorder):
    recorder.renderer.set_theme("AMBER")
    recorder.draw("theme AMBER", recorder.page.state)


def _benchmark_reflow(recorder):
    card = recorder.page.node(layout_page.LayoutRef.CARD)
    rows = layout_page.ROWS
    for frame, width in enumerate((300, 333, 420, 510, 480, 301)):
        def reflow(width=width, frame=frame):
            card.layout_options.width = width
            card.invalidate_layout()
            state = layout_page.LayoutState
            return recorder.page.update_refs(recorder.renderer, {
                state.WIDTH: "%d PX / FRAME %d" % (width, frame),
                state.ROW_ONE: rows[frame % len(rows)],
                state.ROW_TWO: rows[(frame + 1) % len(rows)],
                state.ROW_THREE: rows[(frame + 2) % len(rows)],
            }, [layout_page.LayoutRef.CARD])
        recorder.step("reflow width %d" % width, reflow)


def _file_list():
    recorder = PageRecorder(lambda: file_list.create_page(5, file_list.content_bounds(FeatherRenderer())))
    names = [SimpleNamespace(name="model_%02d_%s.gcode" % (index, "x" * (index * 7)),
                             directory=index % 4 == 0) for index in range(12)]
    for index in (0, 1, 2):
        pagination = Pagination(names, index, 5)
        recorder.step("files page %d" % index,
                      lambda pagination=pagination: file_list.draw(recorder.page, recorder.renderer, pagination))
    recorder.press(lambda page, renderer: file_list.draw(page, renderer, Pagination(names, 1, 5)))
    return recorder.lines


def _settings(record):
    """Run one recording against the product's declared mod parameters."""
    fixture = PreviewSettingsTest()
    fixture.setUp()
    try:
        return record(fixture.manager)
    finally:
        fixture.doCleanups()


def _settings_list(manager):
    """Page through the general settings list on one reused tree."""
    bounds = parameter_controls.content_bounds(FeatherRenderer(), kind="list")
    width = parameter_controls.list_width(bounds)
    pitch = lambda index, param: parameter_controls.parameter_pitch(param, width)
    pages = mod.category_pages(manager, mod.visible_parameters(manager), capacity=bounds.height, item_pitch=pitch)

    def values(index):
        return parameter_controls.list_state(
            manager, pages[index], index, len(pages),
            mod.next_category_hint(pages, index, capacity=bounds.height, item_pitch=pitch))

    recorder = PageRecorder(lambda: parameter_controls.create_list_tree(bounds=bounds))
    recorder.draw("draw page 0", values(0))
    recorder.reflect("reflect page 0")
    for index in tuple(range(1, len(pages))) + (0,):
        recorder.update("page %d" % index, values(index))
    recorder.press(lambda page, renderer: page.draw(renderer, values(0)))
    return recorder.lines


def _choices(manager, key, labels=None):
    param = manager.params_map[key]
    names = labels or mod.enum_names(param)
    return param, [SimpleNamespace(value=name, label=name, description=mod.option_description(param, name),
                                   enabled=True) for name in names]


def _settings_page(create):
    return lambda: _settings(lambda manager: _standard(lambda: create(manager)))


def _options_page(manager):
    param, entries = _choices(manager, "feather_preview_fit")
    bounds = parameter_controls.content_bounds(FeatherRenderer())
    pagination = Pagination(entries, 0, parameter_controls.options_capacity(param, bounds))
    return parameter_controls.create_options_page(param, pagination, entries[1].value, bounds=bounds)


def _theme_page(manager):
    param, entries = _choices(manager, "feather_theme", ("DEFAULT", "AMBER", "DARK", "SYNTH", "CYBERDECK"))
    bounds = parameter_controls.content_bounds(FeatherRenderer())
    pagination = Pagination(entries, 0, parameter_controls.theme_capacity(bounds))
    return parameter_controls.create_theme_page(param, pagination, "AMBER", bounds=bounds)


def _numeric_page(manager):
    return parameter_controls.create_numeric_page(
        manager.params_map["feather_preview_scale"], "1.25",
        bounds=parameter_controls.content_bounds(FeatherRenderer(), kind="numeric"))


def _text_page(symbols, shift):
    return lambda manager: parameter_controls.create_text_page(
        manager.params_map["midi_start"], "MIDI text", 4, symbols, shift,
        bounds=parameter_controls.content_bounds(FeatherRenderer()))


def scenarios():
    """Return scenario name -> factory of recorded lines."""
    result = {}
    for name in MODULE_PAGES:
        extra = []
        if name == "ff5m_ui.exclude_model":
            extra.append(("exclude", _exclude))
        if name == "ff5m_ui.home.page":
            extra.append(("theme", _theme))
        if name == "ff5m_ui.benchmark.layout_page":
            extra.append(("reflow", _benchmark_reflow))
        result[name] = (lambda name=name, extra=tuple(extra):
                        _standard(_module_page(name), extra))
    result["runtime.heat_materials"] = lambda: _standard(
        lambda: heat_page.create_page(("PLA", "PETG", "ABS", "TPU", "PA-CF")))
    result["runtime.filament_material"] = lambda: _standard(
        lambda: copy.deepcopy(filament_runtime.get_material_page(
            (("PLA", 220), ("PETG", 240), ("ABS", 250), ("TPU", 230)))))
    result["runtime.filament_action_pause"] = lambda: _standard(
        lambda: copy.deepcopy(filament_runtime.get_action_page(True)))
    result["runtime.file_list"] = _file_list
    result["settings.list"] = lambda: _settings(_settings_list)
    result["settings.options"] = _settings_page(_options_page)
    result["settings.theme"] = _settings_page(_theme_page)
    result["settings.numeric"] = _settings_page(_numeric_page)
    result["settings.text"] = _settings_page(_text_page(False, False))
    result["settings.text_symbols_shift"] = _settings_page(_text_page(True, True))
    return result
