## Declarative geometry and layout containers for Feather screens.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import copy
import copyreg
import inspect
import itertools
import uuid
from enum import Enum, IntEnum
import functools
from functools import lru_cache

from .engine import (
    LAYOUT_FIELDS, NODE_FIELDS, ActionTable, CommandList, DependencyIndex, EqualTracks, Field, Flex, FrameBuilder,
    GridCell, Insets, LayoutResult, NodeBase, Rect,
    NATIVE_METHODS as _NATIVE, background_repair, behavior_epoch, dirty_roots, normalize_tracks as _normalize_tracks,
    page_structure, paint_state, portable, render_updates, split, update_nodes,
    update_refs as _update_refs,
)
from .bindings import (
    _RESOLVER, Binding, DirectBinding, ItemScope, StateStore, binding_reads, derived, page_state_keys, resolve,
)
from .actions import Command, ItemCommand, action_wire_id, collect_actions, validate_action
from .font_metrics import get_font_metrics
from .identity import PageKey, serialize_key
from .properties import (
    CreationFieldSpec, EditorSpec, Invalidation, PropertySpec, RewritePolicy, SourceSpec,
    ValidationSpec, property_schema,
)


def _capture_construction(instance, names=()):
    return None


def _capture_modifier(node, method, properties):
    return None


def _install_source_hooks(capture_construction, capture_modifier):
    """Install optional Designer provenance hooks on explicit request."""
    global _capture_construction, _capture_modifier
    _capture_construction = capture_construction
    _capture_modifier = capture_modifier



# Rect, Insets, Flex and EqualTracks are immutable engine values; copies are the
# values themselves.
Equal = EqualTracks
FLEX = Flex()


def subdivision_positions(start, span, depth):
    """Return adaptive binary subdivisions including both endpoints."""
    depth = int(depth)
    if depth < 0:
        raise ValueError("Subdivision depth must be non-negative")
    divisions = 1 << depth
    start = int(start)
    span = int(span)
    result = []
    for index in range(divisions + 1):
        coordinate = int(round(start + span * index / float(divisions)))
        if index in (0, divisions):
            level = -1
        else:
            power = 0
            value = index
            while value % 2 == 0:
                power += 1
                value //= 2
            level = max(0, depth - power - 1)
        result.append((index, coordinate, level))
    return tuple(result)


# The engine's node records hold these levels as plain integers (node._dirty).
class Dirty(IntEnum):
    CLEAN = 0
    PAINT = 1
    LAYOUT = 2


class LayoutModifierSpec:
    """Map one fluent source method to framework layout fields."""

    __slots__ = (
        "method", "fields", "argument_codec", "activation", "selection",
    )

    def __init__(self, method, fields, argument_codec="positional_fields",
                 activation="any_non_default", selection="any_changed"):
        self.method = str(method)
        self.fields = tuple(
            (str(name), tuple(int(value) for value in positions))
            for name, positions in fields)
        self.argument_codec = str(argument_codec)
        self.activation = str(activation)
        self.selection = str(selection)

    def as_dict(self):
        return {
            "method": self.method,
            "fields": dict((name, list(positions))
                           for name, positions in self.fields),
            "argument_codec": self.argument_codec,
            "activation": self.activation,
            "selection": self.selection,
        }


class LayoutSourceContract:
    """Framework-owned grammar for fluent layout source editing."""

    __slots__ = ("strategy", "modifiers", "call_site_overrides")

    def __init__(self, strategy, modifiers=(), call_site_overrides=False):
        self.strategy = str(strategy)
        self.modifiers = tuple(modifiers)
        if not all(isinstance(value, LayoutModifierSpec)
                   for value in self.modifiers):
            raise TypeError(
                "LayoutSourceContract modifiers must be LayoutModifierSpec values")
        self.call_site_overrides = bool(call_site_overrides)

    def as_dict(self):
        return {
            "strategy": self.strategy,
            "modifiers": [value.as_dict() for value in self.modifiers],
            "call_site_overrides": self.call_site_overrides,
        }


_FLUENT_LAYOUT_SOURCE = LayoutSourceContract(
    "core.fluent_layout", (
        LayoutModifierSpec(
            "size", (("width", (0,)), ("height", (1,))),
            activation="all_non_default", selection="all_changed"),
        LayoutModifierSpec("width", (("width", (0,)),)),
        LayoutModifierSpec("height", (("height", (0,)),)),
        LayoutModifierSpec("grow", (("grow", (0,)),)),
        LayoutModifierSpec(
            "margin", (("margin", (0,)),), argument_codec="insets"),
        LayoutModifierSpec(
            "padding", (("padding", (0,)),), argument_codec="insets"),
        LayoutModifierSpec(
            "align", (("horizontal", (0,)), ("vertical", (1,))),
            argument_codec="compact_fields"),
        LayoutModifierSpec(
            "offset", (("offset", (0, 1)),), argument_codec="spread_field"),
        LayoutModifierSpec(
            "allow_overflow", (("allow_overflow", (0,)),)),
    ), call_site_overrides=True)


# Every new node's layout_options start with these values. Insets are never
# mutated, so all nodes share the default instances.
_LAYOUT_DEFAULTS = {
    "width": None, "height": None, "grow": 1, "margin": Insets(), "padding": Insets(),
    "horizontal": "stretch", "vertical": "stretch", "offset_x": 0, "offset_y": 0,
    "allow_overflow": False,
}
if set(_LAYOUT_DEFAULTS) != set(LAYOUT_FIELDS):
    raise ImportError("feather_engine layout fields %s differ from the framework defaults" % (LAYOUT_FIELDS,))


def _layout_property(name, runtime_type, default, kind="number", choices=(),
                     minimum=None, maximum=None, nullable=False,
                     source=None, styleable=False, **editor_metadata):
    return PropertySpec(
        name, runtime_type, default=default, nullable=nullable,
        validation=ValidationSpec(
            minimum=minimum, maximum=maximum, choices=choices),
        editor=EditorSpec(
            kind, label=name.replace("_", " ").title(), group="Layout",
            choices=choices, **editor_metadata),
        bindings=(), invalidation=Invalidation.LAYOUT,
        source=SourceSpec(name=source or name, storage="layout"),
        styleable=styleable,
    )


def _validate_size(value):
    if value in ("fill", "content"):
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("size must be fill, content, or an integer")
    if not 1 <= value <= 4000:
        raise ValueError("fixed size must be between 1 and 4000")
    return value


def _size_property(name):
    return PropertySpec(
        name, (int, str), default=None, nullable=True,
        validation=ValidationSpec(validator=_validate_size),
        editor=EditorSpec(
            "size", label=name.title(), group="Layout",
            choices=("fill", "content", "fixed"), wire_codec="size",
            fixed_minimum=1, fixed_maximum=4000),
        bindings=(), invalidation=Invalidation.LAYOUT,
        source=SourceSpec(name=name, storage="layout"), styleable=True)


LAYOUT_SCHEMA = property_schema(
    _size_property("width"),
    _size_property("height"),
    _layout_property(
        "grow", int, 1, minimum=0, maximum=100, wire_codec="integer", styleable=True,
        help="Used only when this axis is Fill in the parent flow."),
    _layout_property(
        "margin", (tuple, list), (0, 0, 0, 0), kind="insets", styleable=True,
        wire_codec="insets", vector_labels=("Left", "Top", "Right", "Bottom"),
        item_minimum=0, item_maximum=2000),
    _layout_property(
        "padding", (tuple, list), (0, 0, 0, 0), kind="insets", styleable=True,
        wire_codec="insets", vector_labels=("Left", "Top", "Right", "Bottom"),
        item_minimum=0, item_maximum=2000),
    _layout_property(
        "horizontal", str, "stretch", kind="select",
        choices=("stretch", "left", "center", "right"),
        wire_codec="identity"),
    _layout_property(
        "vertical", str, "stretch", kind="select",
        choices=("stretch", "top", "center", "bottom"),
        wire_codec="identity"),
    _layout_property(
        "offset", (tuple, list), (0, 0), kind="point",
        wire_codec="point", vector_labels=("X", "Y"),
        item_minimum=-4000, item_maximum=4000,
        runtime_guard="overlay_child_axes",
        help=("For a Fill-sized Overlay child, a positive offset consumes "
              "space on that axis and the child fills the remainder of its slot.")),
    _layout_property(
        "allow_overflow", bool, False, kind="checkbox", wire_codec="boolean",
        help=("Permits an explicit size or offset to draw beyond the parent "
              "slot. It does not resize the parent or fit wrapped content.")),
)


class _PageStructure:
    """Facts about a page tree that change only with its structure.

    The page builds it whenever it consumes a structure invalidation (see
    ``Node.invalidate_actions``) and partial updates trust it until the next
    one. ``nodes`` lists the render tree in paint order and ``ends[i]`` is the
    index after node i's subtree. ``interactive`` holds the ids of nodes whose
    subtree replays input. ``replayable`` is false when a node carries actions
    without an ``interaction_commands`` replay, so only a full draw can
    restore its input.
    """

    __slots__ = ("nodes", "ids", "ends", "painting", "interactive", "replayable")

    def __init__(self, root):
        (self.nodes, self.ids, self.ends, self.painting, self.interactive,
         self.replayable) = page_structure(root)


def _dependency_index(structure, previous=None):
    """Index the state keys read by each node's declared render data (C1).

    The page builds it with its structure. Before an update the page requests
    an update for every node that reads a changed key and for nodes whose
    reads are not declared: ListViews, which update their item scopes, and
    product classes with their own ``state_signature`` or ``blocks_input``.
    The engine marks their ancestors, so the walk skips every clean subtree
    nothing requested. Setting a public property makes the engine mark the
    node stale; its next trusted update refreshes its keys.
    """
    index = DependencyIndex()
    nodes, ends = structure.nodes, structure.ends
    position, count = 0, len(nodes)
    while position < count:
        node = nodes[position]
        if isinstance(node, ListView):
            index.always(node)
            position = ends[position]
            continue
        if type(node)._reads_unindexed_state:
            index.always(node)
        if previous is None or node._dependencies_stale or not index.adopt(previous, node):
            node._refresh_dependencies(index)
        position += 1
    return index


def _attributes(node):
    """The node's declared fields, then its instance attributes, as (name, value) pairs."""
    return node._fields() + tuple(node.__dict__.items())


class CreationIdentityContract:
    """Framework-owned stable identity emitted after node construction."""

    __slots__ = (
        "strategy", "field", "method", "required", "suggestion", "pattern",
    )

    def __init__(self, strategy="core.fluent_ref", field="ref", method="ref",
                 required=True, suggestion="type_counter",
                 pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$"):
        self.strategy = str(strategy)
        self.field = str(field)
        self.method = str(method)
        self.required = bool(required)
        self.suggestion = str(suggestion)
        self.pattern = str(pattern)

    def as_dict(self):
        return {
            "strategy": self.strategy,
            "field": self.field,
            "method": self.method,
            "required": self.required,
            "suggestion": self.suggestion,
            "pattern": self.pattern,
        }


class CreationSourceContract:
    """Framework-owned source generation strategy for a creatable node."""

    __slots__ = ("strategy", "callable", "identity")

    def __init__(self, strategy, callable=None, identity=None):
        self.strategy = str(strategy)
        self.callable = None if callable is None else str(callable)
        if identity is not None and not isinstance(
                identity, CreationIdentityContract):
            raise TypeError(
                "CreationSourceContract identity must be a "
                "CreationIdentityContract")
        self.identity = identity

    def as_dict(self):
        return {
            "strategy": self.strategy,
            "callable": self.callable,
            "identity": (
                None if self.identity is None else self.identity.as_dict()),
        }


class CreationContract:
    """Portable framework-owned construction form for generic tools.

    It describes only how a framework node can be instantiated safely. Product
    behavior, page placement and domain defaults remain in product source.
    """

    __slots__ = ("category", "kind", "fields", "children", "source")

    def __init__(self, category, kind="component", fields=(), children=False,
                 source=None):
        self.category = str(category)
        self.kind = str(kind)
        self.fields = tuple(fields)
        if not all(isinstance(value, CreationFieldSpec) for value in self.fields):
            raise TypeError(
                "CreationContract v2 fields must be CreationFieldSpec values")
        self.children = bool(children)
        if source is not None and not isinstance(source, CreationSourceContract):
            raise TypeError(
                "CreationContract source must be a CreationSourceContract")
        self.source = source

    def field(self, name):
        """Return an explicitly declared field for typed reusable parameters."""
        for value in self.fields:
            if value.name == name:
                return value
        raise ValueError("Unknown creation field: %s" % name)

    def as_dict(self):
        return {
            "category": self.category,
            "kind": self.kind,
            "fields": [value.as_dict() for value in self.fields],
            "children": self.children,
            "source": None if self.source is None else self.source.as_dict(),
        }


class StructureSourceContract:
    """Framework-owned source grammar for structural operations."""

    __slots__ = ("strategy", "supported_forms")

    def __init__(self, strategy, supported_forms=()):
        self.strategy = str(strategy)
        self.supported_forms = tuple(str(value) for value in supported_forms)

    def as_dict(self):
        return {
            "strategy": self.strategy,
            "supported_forms": list(self.supported_forms),
        }


class StructureContract:
    """Portable structural editing capabilities for layout containers."""

    __slots__ = (
        "kind", "source", "operations", "minimum_children",
        "maximum_children", "supports_spans", "placement", "reorder", "canvas",
    )

    def __init__(self, kind, source, operations=("insert", "move", "delete",
                 "extract", "duplicate", "clipboard_insert", "move_many"),
                 minimum_children=0, maximum_children=None, supports_spans=False,
                 placement="flow", reorder=True, canvas=()):
        self.kind = str(kind)
        if not isinstance(source, StructureSourceContract):
            raise TypeError(
                "StructureContract source must be a StructureSourceContract")
        self.source = source
        self.operations = tuple(str(value) for value in operations)
        self.minimum_children = int(minimum_children)
        self.maximum_children = (None if maximum_children is None
                                 else int(maximum_children))
        self.supports_spans = bool(supports_spans)
        self.placement = str(placement)
        self.reorder = bool(reorder)
        self.canvas = tuple(str(value) for value in canvas)

    def as_dict(self):
        return {
            "kind": self.kind,
            "source": self.source.as_dict(),
            "operations": list(self.operations),
            "minimum_children": self.minimum_children,
            "maximum_children": self.maximum_children,
            "supports_spans": self.supports_spans,
            "placement": self.placement,
            "reorder": self.reorder,
            "canvas": list(self.canvas),
        }


_SEQUENCE_STRUCTURE = StructureContract(
    "sequence", StructureSourceContract(
        "core.variadic_children",
        ("inline_arguments", "named_local_collection")),
    minimum_children=0, placement="flow",
    canvas=("flow_reorder", "resize", "multi_select"))
_GRID_MATRIX_STRUCTURE = StructureContract(
    "grid", StructureSourceContract(
        "core.grid_matrix", ("inline_matrix", "named_local_matrix")),
    minimum_children=0, supports_spans=True,
    placement="grid", canvas=("grid_drop", "grid_span", "resize", "multi_select"))
_GRID_CELLS_STRUCTURE = StructureContract(
    "grid", StructureSourceContract(
        "core.grid_cells", ("inline_cells", "named_local_cells")),
    minimum_children=0, supports_spans=True,
    placement="grid", canvas=("grid_drop", "grid_span", "resize", "multi_select"))
_GRID_STRUCTURE = _GRID_MATRIX_STRUCTURE
_OVERLAY_STRUCTURE = StructureContract(
    "sequence", StructureSourceContract(
        "core.variadic_children",
        ("inline_arguments", "named_local_collection")),
    minimum_children=0,
    placement="absolute", canvas=(
        "absolute_move", "absolute_resize", "align", "distribute",
        "snapping", "multi_select"))

_SINGLE_CHILD_STRUCTURE = StructureContract(
    "single_child", StructureSourceContract(
        "core.single_child", ("positional_child", "keyword_child", "empty_child")),
    minimum_children=0, maximum_children=1, placement="content", reorder=False,
    canvas=("resize", "multi_select"))

_STABLE_CREATION_IDENTITY = CreationIdentityContract()
_KEYWORD_CREATION_SOURCE = CreationSourceContract(
    "core.keyword_call", identity=_STABLE_CREATION_IDENTITY)
_GRID_CREATION_SOURCE = CreationSourceContract(
    "core.grid_cells", identity=_STABLE_CREATION_IDENTITY)
_LIST_VIEW_CREATION_SOURCE = CreationSourceContract(
    "core.list_view", identity=_STABLE_CREATION_IDENTITY)


_SEQUENCE_CREATION_FIELDS = (
    CreationFieldSpec(
        "gap", int, default=0,
        validation=ValidationSpec(minimum=0, maximum=4000),
        editor=EditorSpec("number", label="Gap", group="Layout"),
        bindings=()),
)
_ROW_CREATION = CreationContract(
    "Layout", "sequence", _SEQUENCE_CREATION_FIELDS, children=True,
    source=_KEYWORD_CREATION_SOURCE)
_COLUMN_CREATION = CreationContract(
    "Layout", "sequence", _SEQUENCE_CREATION_FIELDS, children=True,
    source=_KEYWORD_CREATION_SOURCE)
_OVERLAY_CREATION = CreationContract(
    "Layout", "absolute", (), children=True,
    source=_KEYWORD_CREATION_SOURCE)
_STATE_CASE_CREATION = CreationContract(
    "Conditions", "absolute", (
        CreationFieldSpec(
            "selector", object, required=True,
            editor=EditorSpec(
                "state_binding", label="State selector", group="Condition"),
            bindings=("direct",)),
        CreationFieldSpec(
            "expected", (str, int, float, bool), required=True,
            editor=EditorSpec(
                "state_value", label="Expected value", group="Condition",
                value_of="selector"),
            bindings=()),
    ), children=True, source=_KEYWORD_CREATION_SOURCE)
# ``state_value`` with ``value_of`` declares that the value must be valid for
# the StateSpec of the state bound by the named sibling field.
_STATE_CASE_PROPERTIES = property_schema(
    PropertySpec(
        "selector", object,
        editor=EditorSpec("state_binding", label="State", group="Condition"),
        bindings=("direct",), invalidation=Invalidation.LAYOUT,
        source=SourceSpec(policy=RewritePolicy.DIRECT_BINDING)),
    PropertySpec(
        "expected", (str, int, float, bool),
        editor=EditorSpec(
            "state_value", label="Equals", group="Condition",
            value_of="selector"),
        bindings=(), invalidation=Invalidation.LAYOUT))
_GRID_CREATION = CreationContract("Layout", "grid", (
    CreationFieldSpec(
        "row_count", int, default=2,
        validation=ValidationSpec(minimum=1, maximum=12),
        editor=EditorSpec("number", label="Rows"), bindings=()),
    CreationFieldSpec(
        "column_count", int, default=2,
        validation=ValidationSpec(minimum=1, maximum=12),
        editor=EditorSpec("number", label="Columns"), bindings=()),
    CreationFieldSpec(
        "column_gap", int, default=0,
        validation=ValidationSpec(minimum=0, maximum=4000),
        editor=EditorSpec("number", label="Column gap"), bindings=()),
    CreationFieldSpec(
        "row_gap", int, default=0,
        validation=ValidationSpec(minimum=0, maximum=4000),
        editor=EditorSpec("number", label="Row gap"), bindings=()),
), children=True, source=_GRID_CREATION_SOURCE)
_WRAP_CREATION = CreationContract("Layout", "sequence", (
    CreationFieldSpec(
        "orientation", str, default="horizontal",
        validation=ValidationSpec(choices=("horizontal", "vertical")),
        editor=EditorSpec(
            "select", label="Orientation",
            choices=("horizontal", "vertical")), bindings=()),
    CreationFieldSpec(
        "item_width", int, default=None, nullable=True,
        validation=ValidationSpec(minimum=1, maximum=4000),
        editor=EditorSpec("number", label="Item width"), bindings=()),
    CreationFieldSpec(
        "min_item_width", int, default=None, nullable=True,
        validation=ValidationSpec(minimum=1, maximum=4000),
        editor=EditorSpec("number", label="Minimum item width"), bindings=()),
    CreationFieldSpec(
        "item_height", int, default=None, nullable=True,
        validation=ValidationSpec(minimum=1, maximum=4000),
        editor=EditorSpec("number", label="Item height"), bindings=()),
    CreationFieldSpec(
        "horizontal_gap", int, default=0,
        validation=ValidationSpec(minimum=0, maximum=4000),
        editor=EditorSpec("number", label="Horizontal gap"), bindings=()),
    CreationFieldSpec(
        "vertical_gap", int, default=0,
        validation=ValidationSpec(minimum=0, maximum=4000),
        editor=EditorSpec("number", label="Vertical gap"), bindings=()),
), children=True, source=_KEYWORD_CREATION_SOURCE)
_SPACER_CREATION = CreationContract(
    "Layout", source=_KEYWORD_CREATION_SOURCE)


_UNSET = object()
_LIST_VIEW_SEQUENCE = itertools.count(1)


def _validate_template_value(value, path):
    # Valid data is checked without building element paths for diagnostics.
    if portable(value, (Enum,)):
        return
    if value is None or isinstance(value, (bool, int, float, str, Enum)):
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_template_value(child, "%s[%d]" % (path, index))
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("%s mapping keys must be strings" % path)
            _validate_template_value(child, "%s.%s" % (path, key))
        return
    raise TypeError("%s contains non-portable %s" %
                    (path, type(value).__name__))


def _validate_template_item(value, path="ListView item"):
    if not isinstance(value, dict):
        raise TypeError("%s must be a mapping" % path)
    _validate_template_value(value, path)
    return value



@lru_cache(maxsize=128)
def _constructor_signature(constructor):
    """The signature without self, and the name of its **kwargs parameter or None.

    Parameter.kind and .name are Python properties; reading them once per
    constructor keeps them out of every node construction.
    """
    signature = inspect.signature(constructor)
    parameters = tuple(signature.parameters.values())
    if parameters and parameters[0].name == "self":
        signature = signature.replace(parameters=parameters[1:])
    var_keywords_name = next((parameter.name for parameter in signature.parameters.values()
                              if parameter.kind == inspect.Parameter.VAR_KEYWORD), None)
    return signature, var_keywords_name


def _is_param_ref(value):
    param_type = globals().get("ParamRef")
    return param_type is not None and isinstance(value, param_type)


def _source_specs(cls):
    result = {}
    for spec in getattr(cls, "property_schema", ()):
        result.setdefault(spec.source.name or spec.name, []).append(spec)
    return result


def _param_placeholder(parameter, specs):
    if parameter is not None and parameter.default is not inspect.Parameter.empty:
        return copy.deepcopy(parameter.default)
    for spec in specs:
        if spec.has_default:
            return copy.deepcopy(spec.default)
    return inspect.Parameter.empty


def _construction_arguments(cls, args, kwargs):
    """Return explicit property names, deferred ParamRefs and the call arguments."""
    explicit = set()
    deferred = {}
    try:
        signature, var_keywords_name = _constructor_signature(cls.__init__)
        bound = signature.bind_partial(*args, **kwargs)
        source_specs = None

        var_keywords = ({} if var_keywords_name is None else
                        dict(bound.arguments.get(var_keywords_name, {}) or {}))

        for spec in getattr(cls, "property_schema", ()):
            source_name = spec.source.name or spec.name
            if source_name in bound.arguments or source_name in var_keywords:
                explicit.add(spec.name)

        for name, value in tuple(bound.arguments.items()):
            if name == var_keywords_name:
                values = dict(value or {})
                changed = False
                for key, item in tuple(values.items()):
                    if not _is_param_ref(item):
                        continue
                    if source_specs is None:
                        source_specs = _source_specs(cls)
                    placeholder = _param_placeholder(None, source_specs.get(key, ()))
                    if placeholder is inspect.Parameter.empty:
                        continue
                    deferred[key] = item
                    values[key] = placeholder
                    changed = True
                if changed:
                    bound.arguments[name] = values
                continue
            if not _is_param_ref(value):
                continue
            if source_specs is None:
                source_specs = _source_specs(cls)
            placeholder = _param_placeholder(signature.parameters[name], source_specs.get(name, ()))
            if placeholder is inspect.Parameter.empty:
                continue
            deferred[name] = value
            bound.arguments[name] = placeholder

        if deferred:
            args = bound.args
            kwargs = bound.kwargs
    except (TypeError, ValueError):
        # Opaque custom signatures remain runtime-valid; they simply do not
        # get central explicit/deferred authoring metadata.
        pass
    return explicit, deferred, args, kwargs


def _finish_construction(node, explicit, deferred):
    node._explicit_properties.update(explicit)
    if deferred:
        node._template_constructor_params = dict(deferred)
        source_specs = _source_specs(type(node))
        for source_name, value in deferred.items():
            if hasattr(node, source_name):
                setattr(node, source_name, value)
                continue
            specs = source_specs.get(source_name, ())
            if len(specs) == 1 and specs[0].source.runtime_index is None:
                specs[0].set_on(node, value)
    node._authoring_property_defaults = dict(
        (spec.name, copy.deepcopy(spec.value_from(node)))
        for spec in getattr(node, "property_schema", ()) if spec.styleable)
    node._authoring_layout_defaults = node._snapshot_layout_options()


def _node_construction(init):
    """Capture explicit args and defer ParamRef coercion around the outermost __init__.

    Node classes deliberately have no custom metaclass: CPython 3.7 never gives
    such classes a type attribute cache, so every node attribute lookup would
    walk the MRO. Nested ``super().__init__`` calls pass straight through.
    """
    @functools.wraps(init)
    def __init__(self, *args, **kwargs):
        fields = self.__dict__
        if fields.get("_constructing_node"):
            return init(self, *args, **kwargs)
        explicit, deferred, args, kwargs = _construction_arguments(type(self), args, kwargs)
        fields["_constructing_node"] = True
        try:
            init(self, *args, **kwargs)
        finally:
            fields.pop("_constructing_node", None)
        _finish_construction(self, explicit, deferred)

    __init__._node_construction = True
    return __init__


# Framework methods a subclass defined outside the framework may override.
# Everything else stays with the engine: render/_arrange make the node a
# Python container, arrange places its own box within the offered slot.
PYTHON_NODE_HOOKS = frozenset((
    "draw", "preferred_extent", "content_extent", "minimum_extent",
    "state_signature", "blocks_input", "opaque_background",
    "render", "_arrange", "arrange", "_display_value",
))
_FRAMEWORK_PACKAGE = __name__.rpartition(".")[0]


def _framework_class(cls):
    if cls is NodeBase:
        return True
    module = cls.__module__
    return module == _FRAMEWORK_PACKAGE or module.startswith(_FRAMEWORK_PACKAGE + ".")


def _check_python_hooks(cls):
    if _framework_class(cls):
        return
    for name, value in cls.__dict__.items():
        if (name.startswith("__") or name in PYTHON_NODE_HOOKS
                or not callable(value) and not isinstance(value, (property, staticmethod, classmethod))):
            continue
        if any(name in base.__dict__ for base in cls.__mro__[1:] if _framework_class(base)):
            raise TypeError("%s.%s overrides a framework method; node subclasses may override only %s" % (
                cls.__qualname__, name, ", ".join(sorted(PYTHON_NODE_HOOKS))))


def _storage_name(spec):
    """The node attribute that holds a property, or None for layout options."""
    if spec.source.storage == "kwargs":
        return "kwargs"
    if spec.source.storage == "layout":
        return None
    return spec.source.runtime_name or spec.name


def _declare_fields(cls):
    """Declare the fields the engine stores for the nodes of cls.

    They are the fields of its bases, the storage of its properties, its
    render data and its ``record_fields``. A field gets a ``Field``
    descriptor unless the class already defines it as a data descriptor (an
    accessor that stores through ``_get_field``/``_set_field``).
    """
    names = []
    for base in reversed(cls.__mro__[1:]):
        names.extend(name for name in base.__dict__.get("_engine_fields", ()) if name not in names)
    own = itertools.chain(
        (_storage_name(spec) for spec in cls.property_schema),
        getattr(cls, "render_fields", ()), cls.record_fields)
    names.extend(name for name in own if name is not None and name not in names)
    cls._engine_fields = tuple(names)
    for name in names:
        defined = next((base.__dict__[name] for base in cls.__mro__ if name in base.__dict__), None)
        if not isinstance(defined, Field) and not hasattr(type(defined), "__set__"):
            setattr(cls, name, Field(name))


class Node(NodeBase):
    """Base object for layout containers and renderable components.

    ``NodeBase`` (the native engine) owns ``parent``, the dirty level
    (``_dirty``, ``invalidate``, ``_mark_dirty``, ``clear_dirty``,
    ``is_clean``), the layout revision, ``_repaint_boundary``, input blocking
    (``_blocks_input``, ``input_blocked``, ``_update_input_blocking``), the
    last state signature (``_update_signature``) and the page structure flag
    (``_actions_dirty``, ``invalidate_actions``). It also runs the layout and
    paint walk: ``arrange``, ``_box``, ``render`` and the default hooks
    (``draw``, ``render_children``, the extents, ...), calling the methods a
    Python class overrides.

    Declared fields (see ``_declare_fields``) live in the node's engine record,
    not in ``__dict__``; ``_fields()`` lists them. The engine reads each class's
    hook implementations again whenever the class or a base changes, so a
    class patch takes effect by itself; instance attributes never replace a
    hook.
    """

    covers_bounds = False
    restores_background = False
    paints_pixels = False
    canvas_selectable = True
    property_schema = ()
    structure_contract = None
    creation_contract = None
    layout_source_contract = _FLUENT_LAYOUT_SOURCE
    # Bindable layout properties whose new resolved value relayouts the node.
    # A class that measures its content or switches children decides itself.
    owns_relayout = False
    relayout_fields = ()
    _relayout_sources = ()
    _style_sources = ()
    # Read by the engine when it creates the node's layout_options.
    _layout_defaults = tuple(_LAYOUT_DEFAULTS[name] for name in LAYOUT_FIELDS)
    # Product hooks may read state that no property declares, so the page
    # updates such nodes on every update instead of by their keys.
    _reads_unindexed_state = False
    # Attributes the engine stores in the node record besides the property
    # storage and render data: node structure and engine-read caches. A
    # template item's ``_item_scope`` is the state its subtree paints with.
    # ``key`` is NodeBase's own: the engine keeps the ref it names.
    record_fields = ("_item_scope",)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _check_python_hooks(cls)
        _declare_fields(cls)
        cls._reads_unindexed_state = any(
            name in base.__dict__ for base in cls.__mro__ if not _framework_class(base)
            for name in ("state_signature", "blocks_input"))
        cls.relayout_fields = () if cls.owns_relayout else tuple(
            spec for spec in cls.property_schema
            if spec.bindings and spec.invalidation == Invalidation.LAYOUT)
        # Where the engine's _update_layout_inputs reads each relayout field,
        # as PropertySpec.value_from does.
        cls._relayout_sources = tuple(
            (spec.name, spec.source.runtime_name or spec.name, spec.source.storage == "kwargs",
             spec.source.runtime_index, spec.default if spec.has_default else None)
            for spec in cls.relayout_fields)
        # Where the engine's StyleApplier reads and writes each styleable
        # property, as PropertySpec.value_from and set_on do, and whether a
        # change relayouts.
        cls._style_sources = tuple(
            (spec.name, spec.source.runtime_name or spec.name, spec.source.storage == "kwargs",
             spec.source.runtime_index, spec.default if spec.has_default else None, spec.nullable,
             spec.inheritable, spec.invalidation != Invalidation.PAINT)
            for spec in cls.property_schema if spec.styleable)
        init = cls.__dict__.get("__init__")
        if init is not None and not getattr(init, "_node_construction", False):
            cls.__init__ = _node_construction(init)

    @_node_construction
    def __init__(self, key=None):
        self.key = key
        self._last_signature = _UNSET
        self._source_mutations = {}
        self._explicit_properties = set()
        self._explicit_layout = set()
        self._authoring_property_defaults = {}
        self._authoring_layout_defaults = None
        self._style_id = None
        self._computed_property_origins = {}
        self._computed_layout_origins = {}
        self._template_constructor_params = {}
        self._source = _capture_construction(self)

    def __getstate__(self):
        """Copy protocol state: the instance dictionary, declared fields, subclass slots and the engine record."""
        slots = dict((name, getattr(self, name)) for name in copyreg._slotnames(type(self))
                     if hasattr(self, name))
        options = self.layout_options
        return (self.__dict__, self._fields(), slots, tuple(getattr(options, name) for name in LAYOUT_FIELDS),
                tuple(getattr(self, name) for name in NODE_FIELDS))

    def __setstate__(self, state):
        values, fields, slots, layout, engine = state
        self.__dict__.update(values)
        for name, value in fields:
            self._set_field(name, value)
        options = self.layout_options
        for name, value in zip(LAYOUT_FIELDS, layout):
            setattr(options, name, value)
        # Setting layout fields invalidated the node; the copied engine state replaces that.
        for name, value in itertools.chain(slots.items(), zip(NODE_FIELDS, engine)):
            setattr(self, name, value)

    def _snapshot_layout_options(self):
        value = self.layout_options
        return {
            "width": value.width,
            "height": value.height,
            "grow": value.grow,
            "margin": Insets(value.margin.left, value.margin.top,
                             value.margin.right, value.margin.bottom),
            "padding": Insets(value.padding.left, value.padding.top,
                              value.padding.right, value.padding.bottom),
            "horizontal": value.horizontal,
            "vertical": value.vertical,
            "offset": (value.offset_x, value.offset_y),
            "allow_overflow": value.allow_overflow,
        }

    def style(self, style_id):
        if not isinstance(style_id, Enum):
            raise TypeError("Node.style requires an Enum member")
        _capture_modifier(self, "style", (("style", 0),))
        self._style_id = style_id
        self.invalidate_layout()
        return self

    def props(self, **properties):
        """Override public properties through their typed framework contracts."""
        specs = {spec.name: spec for spec in self.property_schema}
        values = {}
        for name, value in properties.items():
            spec = specs.get(name)
            if spec is None or not spec.rewrite or spec.source.override_method != "props":
                raise ValueError("Property %s has no props() override contract" % name)
            if isinstance(value, Binding):
                if not spec.bindings:
                    raise ValueError("%s does not accept bindings" % name)
            elif not isinstance(value, ParamRef):
                value = spec.validate(value)
            values[name] = value

        _capture_modifier(self, "props", tuple((name, name) for name in values))
        for name, value in values.items():
            specs[name].set_on(self, value)
        self._explicit_properties.update(values)
        if values:
            self.invalidate_actions()
            self.invalidate_layout()
        return self

    # Shared layout modifiers. They deliberately mutate the declaration node
    # so page construction stays compact and does not allocate wrapper trees.
    def ref(self, key):
        _capture_modifier(self, "ref", (("key", 0),))
        self.key = key
        # A ref is page structure: partial updates trust the previous names.
        self.invalidate_actions()
        self.invalidate_layout()
        return self

    def width(self, value):
        _capture_modifier(self, "width", (("width", 0),))
        self._explicit_layout.add("width")
        self.layout_options.width = self._size_value(value)
        self.invalidate_layout()
        return self

    def height(self, value):
        _capture_modifier(self, "height", (("height", 0),))
        self._explicit_layout.add("height")
        self.layout_options.height = self._size_value(value)
        self.invalidate_layout()
        return self

    def size(self, width, height):
        _capture_modifier(self, "size", (("width", 0), ("height", 1)))
        self._explicit_layout.update(("width", "height"))
        self.layout_options.width = self._size_value(width)
        self.layout_options.height = self._size_value(height)
        self.invalidate_layout()
        return self

    @staticmethod
    def _size_value(value):
        if isinstance(value, ParamRef):
            return value
        if value in (None, "fill"):
            return None
        return _validate_size(value)

    def grow(self, value=1):
        _capture_modifier(self, "grow", (("grow", 0),))
        self._explicit_layout.add("grow")
        if isinstance(value, ParamRef):
            self.layout_options.grow = value
        else:
            self.layout_options.grow = int(value)
            if self.layout_options.grow < 0:
                raise ValueError("Element grow must be non-negative")
        self.invalidate_layout()
        return self

    def margin(self, value=0, **kwargs):
        _capture_modifier(self, "margin", (("margin", 0),))
        self._explicit_layout.add("margin")
        if isinstance(value, ParamRef) and not kwargs:
            self.layout_options.margin = value
        else:
            self.layout_options.margin = Insets.from_values(value, **kwargs)
        self.invalidate_layout()
        return self

    def padding(self, value=0, **kwargs):
        _capture_modifier(self, "padding", (("padding", 0),))
        self._explicit_layout.add("padding")
        if isinstance(value, ParamRef) and not kwargs:
            self.layout_options.padding = value
        else:
            self.layout_options.padding = Insets.from_values(value, **kwargs)
        self.invalidate_layout()
        return self

    def align(self, horizontal=None, vertical=None):
        _capture_modifier(
            self, "align", (("horizontal", 0), ("vertical", 1)))
        if horizontal is not None:
            self._explicit_layout.add("horizontal")
            self.layout_options.horizontal = horizontal
        if vertical is not None:
            self._explicit_layout.add("vertical")
            self.layout_options.vertical = vertical
        self.invalidate_layout()
        return self

    def offset(self, x=0, y=0):
        """Offset an explicitly sized element inside its arranged slot.

        Offsets are primarily useful for Overlay children. Flow containers still
        own their children slots, so editor tooling only exposes this modifier
        when moving the element cannot silently rewrite Grid/List structure.
        """
        _capture_modifier(self, "offset", (("offset", 0),))
        self._explicit_layout.add("offset")
        self.layout_options.offset_x = x if isinstance(x, ParamRef) else int(x)
        self.layout_options.offset_y = y if isinstance(y, ParamRef) else int(y)
        self.invalidate_layout()
        return self

    def allow_overflow(self, value=True):
        _capture_modifier(
            self, "allow_overflow", (("allow_overflow", 0),))
        self._explicit_layout.add("allow_overflow")
        self.layout_options.allow_overflow = (
            value if isinstance(value, ParamRef) else bool(value))
        self.invalidate_layout()
        return self

    def repaint_boundary(self):
        self._repaint_boundary = True
        return self

    # _adopt(*children) is the engine's: the children (None skipped) take the
    # node as their parent, the page re-collects its actions, and membership
    # dirties the flow geometry without advancing the layout revision (explicit
    # layout edits advance the revision that recyclable descendants read).

    # render_dirty(renderer, state, layout) is the engine's: an invalidated
    # subtree repaints with render(). Dynamic leaves may override it when they
    # can restore only their previous damage region more cheaply than
    # redrawing their whole arranged surface.

    # interaction_commands(renderer, state, bounds) is the engine's: no input
    # to replay. Controls replace it to replay their input after a reflow.

    # render_interactions(renderer, state, layout) is the engine's: the input
    # replay of the painted subtree in paint order, each node's
    # interaction_commands in its _item_scope when it has one.

    def replace_preview_children(self, children, placements=None):
        """Replace children on a cloned Designer tree through the framework.

        Product runtime never calls this method. Generic tools use it only on
        deep-copied pages, so container-specific storage stays framework-owned.
        """
        raise TypeError("%s does not publish preview structure editing" %
                        self.__class__.__name__)

    def preview_child_placements(self):
        return None

    # update(state, initialize=False) is NodeBase's: the engine compares the
    # state signature, calls _update_layout_inputs when it is new or changed
    # and the class has relayout fields, updates the render children and sets
    # input blocking. Only the page's own update (update_nodes) trusts its
    # state; there it refreshes stale dependencies and skips clean subtrees.

    def _refresh_dependencies(self, index):
        """Index the state keys the node's declared render data reads and
        the derived bindings it holds."""
        index.refresh(self, *binding_reads(_attributes(self)))

    # _update_layout_inputs(state, baseline) is NodeBase's: the relayout
    # fields resolved and kept as _layout_inputs, a change invalidating the
    # layout unless baseline.
    _resolve = staticmethod(resolve)

    def layout_input(self, name):
        """Resolved value of a relayout field as of the last update."""
        inputs = self.__dict__.get("_layout_inputs")
        if inputs is not None and name in inputs:
            return inputs[name]
        spec = next(spec for spec in self.relayout_fields if spec.name == name)
        return spec.default if spec.has_default else None

    # walk() and walk_declarations() are the engine's: the node and its render
    # subtree as a list. A class whose declarations are not all render
    # children replaces walk_declarations.

    def apply_override(self, name, value):
        for child in self.render_children():
            child.apply_override(name, value)


_declare_fields(Node)


class Template:
    """A named declarative subtree materialized by a data-driven container."""

    def __init__(self, name, root, sample=None):
        if not isinstance(name, str) or not name.strip():
            raise TypeError("Template name must be a non-empty string")
        if not isinstance(root, Node):
            raise TypeError("Template root must be a Node")
        if sample is not None:
            _validate_template_item(sample, "Template sample")
        self.name = name.strip()
        self.root = root
        self.sample = None if sample is None else copy.deepcopy(dict(sample))
        self._source = _capture_construction(self, names=("Template",))



class Param:
    """Named parameter declared by a ComponentTemplate."""

    __slots__ = ("name", "field")

    def __init__(self, name, field=None):
        if not isinstance(name, str) or not name.strip():
            raise TypeError("Param name must be a non-empty string")
        self.name = name.strip()
        if field is not None and not isinstance(field, CreationFieldSpec):
            raise TypeError("Param field must be an explicit CreationFieldSpec")
        self.field = field

    def __repr__(self):
        return "Param(%r)" % self.name


class ParamRef:
    """Framework expression referring to one ComponentTemplate parameter."""

    __slots__ = ("parameter",)

    def __init__(self, parameter):
        if not isinstance(parameter, Param):
            raise TypeError("param() requires a Param")
        self.parameter = parameter


def param(parameter):
    return ParamRef(parameter)


def _template_id(value):
    if not isinstance(value, Enum):
        raise TypeError("ComponentTemplate identity must be an Enum member")
    return value.value


def _substitute_param_value(value, arguments):
    if isinstance(value, ParamRef):
        return arguments[value.parameter.name]
    if isinstance(value, Node):
        return value
    if isinstance(value, tuple):
        return tuple(_substitute_param_value(item, arguments) for item in value)
    if isinstance(value, list):
        return [_substitute_param_value(item, arguments) for item in value]
    if isinstance(value, dict):
        return dict((key, _substitute_param_value(item, arguments))
                    for key, item in value.items())
    return value


def _materialize_insets(value):
    if isinstance(value, Insets):
        return value
    if isinstance(value, (tuple, list)):
        if len(value) != 4:
            raise ValueError("Template margin/padding requires four inset values")
        return Insets(*value)
    return Insets.all(value)


def _substitute_layout_params(node, arguments):
    options = node.layout_options
    width = _substitute_param_value(options.width, arguments)
    height = _substitute_param_value(options.height, arguments)
    grow = _substitute_param_value(options.grow, arguments)
    margin = _substitute_param_value(options.margin, arguments)
    padding = _substitute_param_value(options.padding, arguments)
    horizontal = _substitute_param_value(options.horizontal, arguments)
    vertical = _substitute_param_value(options.vertical, arguments)
    offset_x = _substitute_param_value(options.offset_x, arguments)
    offset_y = _substitute_param_value(options.offset_y, arguments)
    allow_overflow = _substitute_param_value(options.allow_overflow, arguments)

    options.width = node._size_value(width)
    options.height = node._size_value(height)
    options.grow = int(grow)
    if options.grow < 0:
        raise ValueError("Element grow must be non-negative")
    options.margin = _materialize_insets(margin)
    options.padding = _materialize_insets(padding)
    options.horizontal = horizontal
    options.vertical = vertical
    options.offset_x = int(offset_x)
    options.offset_y = int(offset_y)
    options.allow_overflow = bool(allow_overflow)


def _finalize_template_node(node):
    """Apply deferred constructor semantics after ParamRef substitution."""
    deferred = dict(getattr(node, "_template_constructor_params", {}) or {})

    if isinstance(node, StateCase):
        node.predicate = node._binding_predicate()

    if isinstance(node, Grid):
        if "gap" in deferred:
            node.column_gap, node.row_gap = node._gaps(deferred["gap"])
        if "columns" in deferred:
            node.columns = _normalize_tracks(node.columns)
        if "rows" in deferred:
            node.rows = _normalize_tracks(node.rows)
        if (len(node.columns) != node._grid_column_count
                or len(node.rows) != node._grid_row_count):
            raise ValueError("Grid tracks must match the visual extent")

    source_specs = {}
    for spec in getattr(node, "property_schema", ()):
        source_specs.setdefault(spec.source.name or spec.name, []).append(spec)
    for source_name, raw_value in deferred.items():
        specs = source_specs.get(source_name, ())
        if not specs:
            continue
        if (len(specs) > 1 and source_name != "gap"
                and any(spec.source.index is not None for spec in specs)):
            indexes = [spec.source.index for spec in specs
                       if spec.source.index is not None]
            if (not isinstance(raw_value, (tuple, list))
                    or (indexes and max(indexes) >= len(raw_value))):
                raise ValueError(
                    "Template parameter %s does not match its source shape" %
                    source_name)
        for spec in specs:
            value = spec.value_from(node)
            if isinstance(value, Binding):
                if not spec.bindings:
                    raise ValueError(
                        "%s does not accept bindings" % spec.name)
                continue
            spec.validate(value)

    node._template_constructor_params = {}
    if hasattr(node, "_measurement"):
        node._measurement = None


class ComponentTemplate:
    """Declarative reusable subtree with explicit framework parameters."""

    def __deepcopy__(self, memo):
        # Runtime subtrees are copied, but their source declaration remains a
        # shared identity (including when ListView materializes its items).
        memo[id(self)] = self
        return self

    def __init__(self, template_id, parameters, root):
        _template_id(template_id)
        parameters = tuple(parameters or ())
        if not all(isinstance(value, Param) for value in parameters):
            raise TypeError("ComponentTemplate parameters must contain Param values")
        names = [value.name for value in parameters]
        if len(names) != len(set(names)):
            raise ValueError("ComponentTemplate parameter names must be unique")
        if "instance_key" in names:
            raise ValueError("instance_key is reserved for component instance identity")
        if not isinstance(root, Node):
            raise TypeError("ComponentTemplate root must be a Node")
        self.id = template_id
        self.parameters = parameters
        self.root = root
        self._source = _capture_construction(self, names=("ComponentTemplate",))

    def __call__(self, *, instance_key=None, **kwargs):
        expected = tuple(value.name for value in self.parameters)
        missing = [name for name in expected if name not in kwargs]
        extra = [name for name in kwargs if name not in expected]
        if missing:
            raise TypeError("Missing component parameter(s): %s" % ", ".join(missing))
        if extra:
            raise TypeError("Unknown component parameter(s): %s" % ", ".join(extra))
        arguments = dict((name, kwargs[name]) for name in expected)
        # Parameters belong to the shared declaration. Instantiation substitutes
        # their references; copying their editor schemas serves no runtime use.
        root = copy.deepcopy(self.root, {id(value): value for value in self.parameters})
        definition_root_source = getattr(root, "_source", None)

        instance_source = _capture_construction(
            root, names=(self.id.name, "ComponentTemplate"))
        if instance_key is not None:
            if isinstance(instance_key, Enum):
                instance_token = "enum:" + serialize_key(instance_key)
            elif type(instance_key) in (str, int):
                instance_token = "%s:%r" % (type(instance_key).__name__, instance_key)
            else:
                raise TypeError("Component instance_key must be a string, integer or Enum")
        else:
            # Unkeyed instances have runtime-only identity. In particular, a
            # source-site occurrence must never become a persistent ref.
            instance_token = "runtime:" + uuid.uuid4().hex
        scope = "%s[%s]" % (_template_id(self.id), instance_token)

        for node in root.walk():
            _substitute_layout_params(node, arguments)
            original_key = node.key
            if isinstance(original_key, ParamRef):
                node.key = arguments[original_key.parameter.name]
            elif original_key is not None:
                local = original_key.value if isinstance(original_key, Enum) else original_key
                node.key = "%s::%s" % (scope, local)
            elif node is root and instance_key is not None:
                node.key = scope

            for name, value in _attributes(node):
                if name in ("parent", "key", "_source"):
                    continue
                replaced = _substitute_param_value(value, arguments)
                if replaced is not value:
                    setattr(node, name, replaced)
            _finalize_template_node(node)

            metadata = {
                "template": _template_id(self.id),
                "symbol": "%s.%s" % (self.id.__class__.__name__, self.id.name),
                "root": node is root,
                "instance_key": instance_key,
                "instance_scope": scope,
                "parameters": dict(arguments) if node is root else None,
            }
            node._component_template_instance = metadata
            node._component_template = self

        if instance_source is not None:
            root._source = instance_source
        root._component_template_definition_root_source = definition_root_source
        # A new instance is not the object returned while building its prototype.
        root._source_return = None
        return root


class SingleChild(Node):
    record_fields = ("child",)

    def __init__(self, child, key=None):
        super().__init__(key=key)
        self.child = child
        self._adopt(child)

    # The child takes the whole content bounds.
    _arrange = _NATIVE["single_child"]["_arrange"]
    render_children = _NATIVE["single_child"]["render_children"]


class Overlay(Node):
    creation_contract = _OVERLAY_CREATION
    structure_contract = _OVERLAY_STRUCTURE
    record_fields = ("children",)
    # Later children paint over earlier ones: the engine's paint policy
    # repaints an Overlay when a later child paints over a dirty one or the
    # background below it is unknown.
    _stacks_children = True

    def __init__(self, *children, **kwargs):
        super().__init__(key=kwargs.get("key"))
        self.children = tuple(children)
        self._adopt(*self.children)

    # Every child takes the whole content bounds.
    _arrange = _NATIVE["overlay"]["_arrange"]
    render_children = _NATIVE["overlay"]["render_children"]

    def replace_preview_children(self, children, placements=None):
        self.children = tuple(children)
        self._adopt(*self.children)


class Spacer(Node):
    creation_contract = _SPACER_CREATION

    pass


class List(Node):
    """Arrange children sequentially using their shared layout options."""

    # A vertical flow content-sizes the rows of a Grid it holds.
    _flow_container = True
    record_fields = ("items",)
    property_schema = property_schema(
        PropertySpec(
            "direction", str, default="horizontal",
            validation=ValidationSpec(choices=("horizontal", "vertical")),
            editor=EditorSpec(
                "select", label="Direction", group="Layout",
                choices=("horizontal", "vertical")),
            bindings=(), invalidation=Invalidation.STRUCTURE),
        PropertySpec(
            "gap", (int, type(None)), default=0, nullable=True,
            validation=ValidationSpec(minimum=0, maximum=4000),
            editor=EditorSpec(
                "number", label="Gap", group="Layout",
                placeholder="Auto", auto_label="Auto"),
            bindings=(), invalidation=Invalidation.LAYOUT))
    structure_contract = _SEQUENCE_STRUCTURE

    def __init__(self, direction, *children, **kwargs):
        super().__init__(key=kwargs.get("key"))
        if direction not in ("horizontal", "vertical"):
            raise ValueError("Unknown list direction: %s" % direction)
        self.direction = direction
        gap = kwargs.get("gap", 0)
        self.gap = None if gap is None else int(gap)
        if self.gap is not None and self.gap < 0:
            raise ValueError("List gap must be non-negative or None")
        self.items = tuple(children)
        self._adopt(*self.items)

    @classmethod
    def horizontal(cls, *children, **kwargs):
        return cls("horizontal", *children, **kwargs)

    @classmethod
    def vertical(cls, *children, **kwargs):
        return cls("vertical", *children, **kwargs)

    # Fixed and content children keep their size plus margin; the others
    # share the free space by grow weight. A child's minimum extent raises its
    # size, and lets a flexible list overflow. Without flexible children,
    # gap=None spreads the free space evenly between the children.
    _arrange = _NATIVE["list"]["_arrange"]
    _arrange_item = _NATIVE["list"]["_arrange_item"]
    preferred_extent = _NATIVE["list"]["preferred_extent"]
    render_children = _NATIVE["list"]["render_children"]

    def replace_preview_children(self, children, placements=None):
        self.items = tuple(children)
        self._adopt(*self.items)


_LIST_VIEW_STRUCTURE = StructureContract(
    "data_driven", StructureSourceContract(
        "core.template_definitions", ("template_declarations",)),
    operations=(), minimum_children=0, placement="flow", reorder=False,
    canvas=("resize", "multi_select"))

_LIST_VIEW_CREATION = CreationContract(
    "Data", "data_driven", (
        CreationFieldSpec(
            "items", object, required=True,
            editor=EditorSpec(
                "state_binding", label="Item source", group="Data"),
            bindings=("direct",)),
        CreationFieldSpec(
            "item_key", str, default="id",
            editor=EditorSpec(
                "item_field", label="Item identity field", group="Data"),
            bindings=()),
        CreationFieldSpec(
            "template_key", str, default=None, nullable=True,
            editor=EditorSpec(
                "item_field", label="Template selector field", group="Data",
                placeholder="Only for several templates"),
            bindings=()),
        CreationFieldSpec(
            "templates", list, required=True, default=({
                "name": "item", "height": 64,
                "sample": {"id": "sample:item"},
            },),
            editor=EditorSpec(
                "template_specs", label="Template definitions", group="Data"),
            bindings=()),
        CreationFieldSpec(
            "direction", str, default="vertical",
            validation=ValidationSpec(choices=("horizontal", "vertical", "overlay")),
            editor=EditorSpec(
                "select", label="Direction", group="Layout",
                choices=("horizontal", "vertical", "overlay")),
            bindings=()),
        CreationFieldSpec(
            "gap", int, default=0,
            validation=ValidationSpec(minimum=0, maximum=4000),
            editor=EditorSpec("number", label="Gap", group="Layout"),
            bindings=()),
        CreationFieldSpec(
            "reuse_items", bool, default=False,
            editor=EditorSpec("checkbox", label="Reuse items", group="Data"),
            bindings=()),
    ), source=_LIST_VIEW_CREATION_SOURCE)


def _clone_template_root(root):
    """Clone ordinary node fields and dictionaries without reconstructing every scalar field.

    Preserve Python's copy protocol for custom slots, allocation and copy hooks.
    One memo keeps parent links and shared fields scoped to the cloned subtree.
    """
    nodes = tuple(root.walk())
    for cls in {type(node) for node in nodes}:
        if (cls.__new__ is not NodeBase.__new__
                or any("__slots__" in base.__dict__ for base in cls.__mro__)
                or getattr(cls, "__copy__", None) is not None
                or getattr(cls, "__deepcopy__", None) is not None
                or cls.__reduce__ is not object.__reduce__
                or cls.__reduce_ex__ is not object.__reduce_ex__
                or cls.__getstate__ is not Node.__getstate__
                or cls.__setstate__ is not Node.__setstate__):
            return copy.deepcopy(root)
    memo = {id(node): NodeBase.__new__(type(node)) for node in nodes}
    scalars = (str, int, float, bool, type(None))
    for node in nodes:
        options = node.layout_options
        clone = memo[id(node)].layout_options
        memo[id(options)] = clone
        for name in LAYOUT_FIELDS:
            value = getattr(options, name)
            setattr(clone, name, value if type(value) in scalars else copy.deepcopy(value, memo))
    for node in nodes:
        clone = memo[id(node)]
        for name, value in node._fields():
            clone._set_field(name, value if type(value) in scalars else copy.deepcopy(value, memo))
        clone.__dict__.update((name, value if type(value) in scalars
                              else copy.deepcopy(value, memo))
                             for name, value in node.__dict__.items())
        for name in NODE_FIELDS:
            value = getattr(node, name)
            setattr(clone, name, value if type(value) in scalars else copy.deepcopy(value, memo))
    return memo[id(root)]


class ListView(List):
    """Materialize named templates, optionally recycling model-owned rows.

    Reuse is opt-in. Recyclable templates keep item state in bindings rather
    than local delegate fields. The detached pool retains at most as many
    instances as the current collection, and is released for an empty list.
    """

    property_schema = property_schema(
        PropertySpec(
            "items", (list, tuple), default=(),
            editor=EditorSpec(
                "collection_binding", label="Item source", group="Data"),
            bindings=("direct", "derived"),
            invalidation=Invalidation.STRUCTURE,
            source=SourceSpec(name="items", runtime_name="items_binding")),
        PropertySpec(
            "item_key", object,
            editor=EditorSpec(
                "item_binding", label="Item identity", group="Data"),
            bindings=("item", "derived"),
            invalidation=Invalidation.STRUCTURE,
            source=SourceSpec(name="item_key", runtime_name="item_key_binding")),
        PropertySpec(
            "template_key", object, nullable=True,
            editor=EditorSpec(
                "item_binding", label="Template selector", group="Data"),
            bindings=("item", "derived"),
            invalidation=Invalidation.STRUCTURE,
            source=SourceSpec(
                name="template_key", runtime_name="template_key_binding")),
        PropertySpec(
            "direction", str, default="vertical",
            validation=ValidationSpec(choices=("horizontal", "vertical", "overlay")),
            editor=EditorSpec(
                "select", label="Direction", group="Layout",
                choices=("horizontal", "vertical", "overlay")),
            bindings=(), invalidation=Invalidation.STRUCTURE),
        PropertySpec(
            "gap", (int, type(None)), default=0, nullable=True,
            validation=ValidationSpec(minimum=0, maximum=4000),
            editor=EditorSpec(
                "number", label="Gap", group="Layout",
                placeholder="Auto", auto_label="Auto"),
            bindings=(), invalidation=Invalidation.LAYOUT),
        PropertySpec(
            "reuse_items", bool, default=False,
            editor=EditorSpec("checkbox", label="Reuse items", group="Data"),
            bindings=(), invalidation=Invalidation.STRUCTURE),
        PropertySpec(
            "templates", tuple, default=(),
            editor=EditorSpec(
                "template_catalog", label="Templates", group="Data"),
            bindings=(), invalidation=Invalidation.STRUCTURE, live=False,
            source=SourceSpec(
                name="templates", runtime_name="template_names",
                policy=RewritePolicy.LOCKED,
                reason="Templates are edited through their declarations")))
    structure_contract = _LIST_VIEW_STRUCTURE
    creation_contract = _LIST_VIEW_CREATION
    # The engine arranges what Designer shows of the list.
    record_fields = ("_designer_mode", "_template_samples")

    def __init__(self, items, item_key, template_key=None, templates=(),
                 direction="vertical", gap=0, fallback_template=None, key=None, reuse_items=False):
        Node.__init__(self, key=key)
        if not isinstance(items, (Binding, list, tuple)):
            raise TypeError("ListView items must be a binding, list or tuple")
        if not isinstance(item_key, Binding):
            raise TypeError("ListView item_key must be a binding")
        if template_key is not None and not isinstance(template_key, Binding):
            raise TypeError("ListView template_key must be a binding or None")
        if direction not in ("horizontal", "vertical", "overlay"):
            raise ValueError("Unknown ListView direction: %s" % direction)
        templates = tuple(templates or ())
        if not templates or not all(isinstance(value, Template)
                                    for value in templates):
            raise TypeError("ListView templates must contain Template values")
        names = [value.name for value in templates]
        if len(names) != len(set(names)):
            raise ValueError("ListView template names must be unique")
        if isinstance(fallback_template, Template):
            fallback_template = fallback_template.name
        if fallback_template is not None and fallback_template not in names:
            raise ValueError("ListView fallback template is not declared")
        if template_key is None and len(templates) > 1 and fallback_template is None:
            # Without a selector every item uses the single declared template
            # (or the explicit fallback); several templates need a selector.
            raise ValueError(
                "ListView template_key is required when several templates "
                "are declared without a fallback_template")
        if not isinstance(reuse_items, bool):
            raise TypeError("ListView reuse_items must be a boolean")
        self.reuse_items = reuse_items
        self._reuse_pool = ()
        self.items_binding = items
        self.item_key_binding = item_key
        self.template_key_binding = template_key
        self.templates = templates
        self.template_names = tuple(value.name for value in templates)
        self.direction = direction
        self.gap = None if gap is None else int(gap)
        if self.gap is not None and self.gap < 0:
            raise ValueError("ListView gap must be non-negative or None")
        self.fallback_template = fallback_template
        self.items = ()
        self._item_scopes = ()
        self._materialized_signature = None
        self._update_state = None
        self._update_keys = frozenset()
        self._designer_mode = False
        self._template_samples = ()
        self._list_scope = self._scope_name()

    def _scope_name(self):
        if self.key is not None:
            return str(self.key.value if isinstance(self.key, Enum) else self.key)
        trace = self._source or {}
        fingerprint = (trace.get("anchor") or {}).get("fingerprint")
        return ("source:%s" % fingerprint if fingerprint else
                "declaration:%d" % next(_LIST_VIEW_SEQUENCE))

    def ref(self, key):
        super().ref(key)
        self._list_scope = self._scope_name()
        return self

    # An item the engine cannot confirm as a mapping of portable values: accepted
    # here, or rejected with the path of the offending value.
    @staticmethod
    def _validate_item(value, index):
        _validate_template_item(value, "ListView item %d" % index)

    # "%s[%s]::%s" % (self._list_scope, item_key, local).
    _scoped_ref = _NATIVE["list_view"]["_scoped_ref"]

    def _prepare_tree(self, root, scope, metadata, definition=False):
        declarations = []
        for node in root.walk():
            commands = tuple((name, value) for name, value in _attributes(node)
                             if self._contains_item_command(value))
            if self.reuse_items and not definition:
                declarations.append((node, node.key, commands))
            is_root = node is root
            if node.key is not None:
                local = node.key.value if isinstance(node.key, Enum) else node.key
                node.key = self._scoped_ref(metadata["item_key"], local)
            if definition:
                node._template_definition = {
                    "template": metadata["template"], "sample": True, "root": is_root,
                }
            else:
                node._template_instance = dict(metadata, root=is_root)
            for name, value in commands:
                setattr(node, name, self._resolve_item_commands(value, scope))
        if self.reuse_items and not definition:
            root._reuse_declarations = tuple(declarations)
        root._item_scope = scope
        return root

    # The value with its ItemCommands resolved for the scope, inside tuples,
    # lists and dicts. Given the field's current value, a resolved command
    # known to equal its current one (same key and hint, an equal plain
    # payload) keeps it, and so does a container whose parts were all kept.
    _resolve_item_commands = _NATIVE["list_view"]["_resolve_item_commands"]
    _item_command_type = ItemCommand
    _command_type = Command
    # The framework's binding and state types; resolves item bindings and makes item scopes.
    _resolver = _RESOLVER

    @classmethod
    def _contains_item_command(cls, value):
        if isinstance(value, ItemCommand):
            return True
        if isinstance(value, (tuple, list)):
            return any(cls._contains_item_command(part) for part in value)
        if isinstance(value, dict):
            return any(cls._contains_item_command(part) for part in value.values())
        return False

    # The items of a list that does not reuse them: fresh clones of the plan's
    # (scope, template, metadata) entries.
    def _fresh_items(self, plan):
        return tuple(self._prepare_tree(_clone_template_root(template.root), scope, metadata)
                     for scope, template, metadata in plan)

    # A current item made from the same template and root for the same item
    # key stays; any other entry takes the first detached or removed item
    # made from its template's root, or a new one. Every item's refs and
    # commands are resolved before any item changes; a changed ref
    # invalidates the item's actions and layout, a changed command its
    # actions. The removed items the templates could reuse, at most as many
    # as the plan's entries, stay in the pool; every removed item is detached.
    _recycle_items = _NATIVE["list_view"]["_recycle_items"]

    def _new_item(self, template, scope, metadata):
        node = self._prepare_tree(_clone_template_root(template.root), scope, metadata)
        node._reuse_template = template
        node._reuse_prototype = template.root
        return node

    def _materialize_samples(self, state):
        if not self._designer_mode:
            return ()
        result = []
        visible = not self.items
        for template in self.templates:
            if template.sample is None:
                continue
            scope = ItemScope(state, template.sample)
            metadata = {
                "template": template.name,
                "item_key": "sample:%s" % template.name,
                "index": None,
            }
            root = self._prepare_tree(
                _clone_template_root(template.root), scope, metadata, definition=True)
            for node in root.walk():
                node._designer_template_visible = visible
            root.parent = self
            result.append(root)
        return tuple(result)

    # A clean list updated again with the same state keeps everything while no
    # key its declarations read changed. Otherwise the items are resolved (a
    # direct collection from the store's own snapshot), scoped and selected by
    # item key and template key. A changed signature (items, selectors and,
    # with reuse_items, the template declarations) materializes the items:
    # unique scalar keys, the selected or fallback template, recycled
    # (_recycle_items) or fresh (_fresh_items) items, adopted when they changed,
    # Designer's template samples; it invalidates the list's paint with reuse,
    # else its layout. Each item and sample then updates in its scope. The state
    # keys are derived again (_state_keys) after an invalidation or a change of
    # the declarations.
    update = _NATIVE["list_view"]["update"]

    def _state_keys(self):
        return frozenset(page_state_keys(self))

    # The rendered items (in Designer, the template samples of an empty
    # list) are arranged as a List, or each over the whole bounds for the
    # overlay direction; template samples not rendered take the whole
    # bounds. With reuse_items, outside Designer and when the layout does not
    # reuse clean geometry, a clean item at the same size under the same
    # ancestors' layout revisions places what it placed before, moved with
    # its bounds; the engine keeps that geometry on the item.
    _arrange = _NATIVE["list_view"]["_arrange"]
    _arrange_item = _NATIVE["list_view"]["_arrange_item"]

    # The painted items are the items, or in Designer the template samples of
    # an empty list; each renders with its item scope. The render children are
    # the items and the samples.
    render = _NATIVE["list_view"]["render"]
    render_children = _NATIVE["list_view"]["render_children"]
    paint_children = _NATIVE["list_view"]["paint_children"]

    def walk_declarations(self):
        """Include template dependencies even before any items exist."""
        nodes = [self]
        for template in self.templates:
            nodes.extend(template.root.walk_declarations())
        for child in self.render_children():
            nodes.extend(child.walk_declarations())
        return nodes

    def replace_preview_children(self, children, placements=None):
        raise TypeError(
            "ListView runtime items are controlled by bound collection data")


class Row(List):
    creation_contract = _ROW_CREATION

    def __init__(self, *children, **kwargs):
        super().__init__("horizontal", *children, **kwargs)


class Column(List):
    creation_contract = _COLUMN_CREATION

    def __init__(self, *children, **kwargs):
        super().__init__("vertical", *children, **kwargs)


class Cell(GridCell):
    """Declarative coordinate-based Grid source value."""

    def __init__(self, child, column, row, column_span=1, row_span=1):
        super().__init__(child, column, row, column_span, row_span)
        if self.column < 0 or self.row < 0:
            raise ValueError("Grid cell coordinates must be non-negative")
        if self.column_span <= 0 or self.row_span <= 0:
            raise ValueError("Grid cell spans must be positive")


class Span:
    """A typed matrix cell spanning adjacent grid columns or rows."""

    __slots__ = ("child", "columns", "rows")

    def __init__(self, child, columns=1, rows=1):
        self.child = child
        self.columns = int(columns)
        self.rows = int(rows)
        if self.columns <= 0 or self.rows <= 0:
            raise ValueError("Grid span must be positive")


class _EmptyCell:
    pass


EMPTY = _EmptyCell()



class Grid(Node):
    """Arrange a typed visual matrix in fixed or flexible tracks."""

    creation_contract = _GRID_CREATION
    record_fields = ("cells",)
    property_schema = property_schema(
        PropertySpec(
            "columns", (tuple, list), default=(),
            editor=EditorSpec("tracks", label="Column tracks", group="Grid"),
            bindings=(), invalidation=Invalidation.STRUCTURE,
            source=SourceSpec(name="columns", position=1)),
        PropertySpec(
            "rows", (tuple, list), default=(),
            editor=EditorSpec("tracks", label="Row tracks", group="Grid"),
            bindings=(), invalidation=Invalidation.STRUCTURE,
            source=SourceSpec(name="rows", position=2)),
        PropertySpec(
            "column_gap", int, default=0,
            validation=ValidationSpec(minimum=0, maximum=4000),
            editor=EditorSpec("number", label="Column gap", group="Grid"),
            bindings=(), invalidation=Invalidation.LAYOUT,
            source=SourceSpec(name="gap", position=3, index=0)),
        PropertySpec(
            "row_gap", int, default=0,
            validation=ValidationSpec(minimum=0, maximum=4000),
            editor=EditorSpec("number", label="Row gap", group="Grid"),
            bindings=(), invalidation=Invalidation.LAYOUT,
            source=SourceSpec(name="gap", position=3, index=1)))
    structure_contract = _GRID_MATRIX_STRUCTURE

    def __init__(self, matrix=None, columns=None, rows=None, gap=0, key=None, *,
                 cells=None):
        super().__init__(key=key)
        if (matrix is None) == (cells is None):
            raise ValueError("Grid requires exactly one of matrix or cells")
        self.column_gap, self.row_gap = self._gaps(gap)
        if cells is not None:
            self.cells, column_count, row_count = self._coordinate_cells(cells)
            self._grid_source_form = "cells"
            self.structure_contract = _GRID_CELLS_STRUCTURE
        else:
            self.cells, column_count, row_count = self._matrix_cells(matrix)
            self._grid_source_form = "matrix"
            self.structure_contract = _GRID_MATRIX_STRUCTURE
        self._grid_column_count = column_count
        self._grid_row_count = row_count
        self.columns = _normalize_tracks(
            EqualTracks(column_count) if columns is None else columns)
        self.rows = _normalize_tracks(
            EqualTracks(row_count) if rows is None else rows)
        if cells is not None:
            if len(self.columns) < column_count or len(self.rows) < row_count:
                raise ValueError("Grid cells exceed the declared tracks")
            column_count, row_count = len(self.columns), len(self.rows)
            if not column_count or not row_count:
                raise ValueError("Empty Grid cells require explicit non-empty tracks")
            self._grid_column_count = column_count
            self._grid_row_count = row_count
        if len(self.columns) != column_count or len(self.rows) != row_count:
            raise ValueError("Grid tracks must match the visual extent")
        self._adopt(*(item.child for item in self.cells))

    @staticmethod
    def _gaps(gap):
        if isinstance(gap, tuple):
            return int(gap[0]), int(gap[1])
        return int(gap), int(gap)

    @staticmethod
    def _matrix_cells(matrix):
        rows = tuple(tuple(row) for row in matrix)
        if not rows:
            raise ValueError("Grid matrix must not be empty")
        columns = max(len(row) for row in rows)
        if columns <= 0 or any(len(row) != columns for row in rows):
            raise ValueError("Grid matrix rows must have equal length")
        occupied = set()
        cells = []
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                if value is None or value is EMPTY:
                    continue
                if (column_index, row_index) in occupied:
                    raise ValueError("Grid matrix cell overlaps a span")
                if isinstance(value, Span):
                    child = value.child
                    column_span = value.columns
                    row_span = value.rows
                else:
                    child = value
                    column_span = row_span = 1
                if (column_index + column_span > columns
                        or row_index + row_span > len(rows)):
                    raise ValueError("Grid span is outside the matrix")
                for occupied_row in range(row_index, row_index + row_span):
                    for occupied_column in range(
                            column_index, column_index + column_span):
                        coordinate = (occupied_column, occupied_row)
                        if coordinate in occupied:
                            raise ValueError("Grid matrix spans overlap")
                        occupied.add(coordinate)
                cells.append(GridCell(
                    child, column_index, row_index, column_span, row_span))
        return tuple(cells), columns, len(rows)

    @staticmethod
    def _coordinate_cells(cells):
        declared = tuple(cells)
        occupied = set()
        normalized = []
        column_count = 0
        row_count = 0
        for value in declared:
            if not isinstance(value, GridCell):
                raise TypeError("Grid cells must contain Cell values")
            if not isinstance(value.child, Node):
                raise TypeError("Grid cell child must be a Node")
            if value.column < 0 or value.row < 0:
                raise ValueError("Grid cell coordinates must be non-negative")
            if value.column_span <= 0 or value.row_span <= 0:
                raise ValueError("Grid cell spans must be positive")
            for occupied_row in range(value.row, value.row + value.row_span):
                for occupied_column in range(
                        value.column, value.column + value.column_span):
                    coordinate = (occupied_column, occupied_row)
                    if coordinate in occupied:
                        raise ValueError("Grid cells overlap")
                    occupied.add(coordinate)
            normalized.append(GridCell(
                value.child, value.column, value.row,
                value.column_span, value.row_span))
            column_count = max(
                column_count, value.column + value.column_span)
            row_count = max(row_count, value.row + value.row_span)
        return tuple(normalized), column_count, row_count

    # Cells take the area of their column and row spans. Under a vertical
    # List that content-sizes the grid, rows follow the heights their cells
    # need, with flexible rows growing by weight; otherwise flexible rows
    # share the height. The intrinsic height is also the minimum, so row
    # bounds keep describing the pixels their children paint.
    _arrange = _NATIVE["grid"]["_arrange"]
    preferred_extent = _NATIVE["grid"]["preferred_extent"]
    minimum_extent = _NATIVE["grid"]["minimum_extent"]
    render_children = _NATIVE["grid"]["render_children"]
    grid_areas = _NATIVE["grid"]["grid_areas"]

    def replace_preview_children(self, children, placements=None):
        placements = tuple(placements or ())
        if len(children) != len(placements):
            raise ValueError("Grid preview children need explicit placements")
        cells = tuple(GridCell(
            child, value.get("column", 0), value.get("row", 0),
            value.get("column_span", 1), value.get("row_span", 1))
            for child, value in zip(children, placements))
        cells, column_count, row_count = self._coordinate_cells(cells)
        if column_count > len(self.columns) or row_count > len(self.rows):
            raise ValueError("Grid preview cells exceed the declared tracks")
        self.cells = cells
        self._adopt(*tuple(children))

    def preview_child_placements(self):
        return tuple({
            "column": item.column, "row": item.row,
            "column_span": item.column_span, "row_span": item.row_span,
        } for item in self.cells)


class WrapPanel(Node):
    creation_contract = _WRAP_CREATION
    structure_contract = _SEQUENCE_STRUCTURE
    record_fields = ("children",)
    property_schema = property_schema(
        PropertySpec(
            "orientation", str, default="horizontal",
            validation=ValidationSpec(choices=("horizontal", "vertical")),
            editor=EditorSpec(
                "select", label="Orientation", group="Wrap layout",
                choices=("horizontal", "vertical")),
            bindings=(), invalidation=Invalidation.STRUCTURE),
        PropertySpec(
            "item_width", int, default=None, nullable=True,
            validation=ValidationSpec(minimum=1, maximum=4000),
            editor=EditorSpec(
                "number", label="Item width", group="Wrap layout"),
            bindings=(), invalidation=Invalidation.LAYOUT),
        PropertySpec(
            "min_item_width", int, default=None, nullable=True,
            validation=ValidationSpec(minimum=1, maximum=4000),
            editor=EditorSpec(
                "number", label="Minimum item width", group="Wrap layout"),
            bindings=(), invalidation=Invalidation.LAYOUT),
        PropertySpec(
            "item_height", int, default=None, nullable=True,
            validation=ValidationSpec(minimum=1, maximum=4000),
            editor=EditorSpec(
                "number", label="Item height", group="Wrap layout"),
            bindings=(), invalidation=Invalidation.LAYOUT),
        PropertySpec(
            "horizontal_gap", int, default=0,
            validation=ValidationSpec(minimum=0, maximum=4000),
            editor=EditorSpec(
                "number", label="Horizontal gap", group="Wrap layout"),
            bindings=(), invalidation=Invalidation.LAYOUT),
        PropertySpec(
            "vertical_gap", int, default=0,
            validation=ValidationSpec(minimum=0, maximum=4000),
            editor=EditorSpec(
                "number", label="Vertical gap", group="Wrap layout"),
            bindings=(), invalidation=Invalidation.LAYOUT))

    """Lay out equal-sized children and wrap them across rows or columns."""

    def __init__(self, *children, **kwargs):
        super().__init__(key=kwargs.get("key"))
        self.children = tuple(children)
        self.orientation = kwargs.get("orientation", "horizontal")
        if self.orientation not in ("horizontal", "vertical"):
            raise ValueError("Unknown wrap orientation: %s" % self.orientation)
        self.item_width = kwargs.get("item_width")
        self.min_item_width = kwargs.get("min_item_width")
        self.item_height = kwargs.get("item_height")
        self.horizontal_gap = int(kwargs.get("horizontal_gap", 0))
        self.vertical_gap = int(kwargs.get("vertical_gap", 0))
        self._adopt(*self.children)

    # Equal items in rows (or columns), as many per row as fit their width
    # and gap; without an item width the items share the row.
    _arrange = _NATIVE["wrap_panel"]["_arrange"]
    render_children = _NATIVE["wrap_panel"]["render_children"]

    def replace_preview_children(self, children, placements=None):
        self.children = tuple(children)
        self._adopt(*self.children)


class When(SingleChild):
    """Render a child only when the state predicate is true."""

    owns_relayout = True
    # The engine paints the branch from the predicate it stores; it resolves
    # a direct binding there from the page's state.
    record_fields = ("predicate",)
    _direct_binding = DirectBinding

    def __init__(self, predicate, child, key=None):
        super().__init__(child, key=key)
        self.predicate = predicate

    # state_signature(state) is the engine's: bool(resolve(predicate, state)).
    state_signature = _NATIVE["when"]["state_signature"]

    # update(state, initialize=False) is the engine's. Hidden branches catch up
    # when shown; evaluating their bindings on every telemetry sample adds work
    # without changing any pixels. Otherwise the node updates as any node, and
    # a branch the update hid calls _branch_hidden.
    update = _NATIVE["when"]["update"]

    def _branch_hidden(self, previous):
        # Showing a branch registers its controls normally. Hiding must remove
        # old regions, which Typer only supports per whole layer.
        if previous is not _UNSET and collect_actions(self.child):
            self.invalidate_actions()

    # render and paint_children are the engine's: the child (its commands)
    # while the predicate holds, else nothing.
    render = _NATIVE["when"]["render"]
    paint_children = _NATIVE["when"]["paint_children"]


class StateCase(When):
    """Designer-friendly conditional Overlay selected by a state value.

    Children are ordinary positional source arguments while ``selector`` and
    ``expected`` stay explicit keyword fields. This makes empty creation and
    later contract-driven insertion deterministic without parsing a lambda.
    """

    creation_contract = _STATE_CASE_CREATION
    structure_contract = _OVERLAY_STRUCTURE
    property_schema = _STATE_CASE_PROPERTIES

    def __init__(self, *children, **kwargs):
        if "selector" not in kwargs or "expected" not in kwargs:
            raise TypeError("StateCase requires selector and expected")
        self._set_field("selector", kwargs.pop("selector"))
        self._set_field("expected", kwargs.pop("expected"))
        if kwargs.keys() - {"key"}:
            raise TypeError("Unknown StateCase arguments: %s" %
                            ", ".join(sorted(kwargs)))
        super().__init__(self._predicate(), Overlay(*children),
                         key=kwargs.get("key"))

    # selector and expected are stored in the engine, which compares them to
    # paint the branch; assigning one rebuilds the predicate.
    @property
    def selector(self):
        return self._get_field("selector")

    @selector.setter
    def selector(self, value):
        self._set_field("selector", value)
        self.predicate = self._predicate()
        self.invalidate_layout()

    @property
    def expected(self):
        return self._get_field("expected")

    @expected.setter
    def expected(self, value):
        self._set_field("expected", value)
        self.predicate = self._predicate()
        self.invalidate_layout()

    def _predicate(self):
        # Parameter declarations are materialized before a template can render.
        # derived() deliberately accepts only concrete Binding inputs.
        if isinstance(self.selector, ParamRef) or isinstance(self.expected, ParamRef):
            return False
        return self._binding_predicate()

    def _binding_predicate(self):
        return derived(
            lambda current, expected=self.expected: current == expected,
            self.selector)

    # The engine's: state_signature is resolve(selector, state) ==
    # resolve(expected, state); render draws the child while the signature
    # holds; the render children are the overlay's; paint_children is When's
    # (the predicate), which the engine's core also answers.
    state_signature = _NATIVE["state_case"]["state_signature"]
    render = _NATIVE["state_case"]["render"]
    render_children = _NATIVE["state_case"]["render_children"]
    paint_children = _NATIVE["state_case"]["paint_children"]

    def replace_preview_children(self, children, placements=None):
        self.child.replace_preview_children(children, placements)
        self._adopt(*tuple(children))


class Override:
    """Apply explicit inherited defaults to an existing object subtree."""

    def __init__(self, content):
        self.content = content
        self.values = []

    def with_font(self, font):
        self.values.append(("font", font))
        return self

    def with_text_color(self, color):
        self.values.append(("text_color", color))
        return self

    def with_button_style(self, style):
        self.values.append(("button_style", style))
        return self

    def apply(self):
        for name, value in self.values:
            self.content.apply_override(name, value)
        return self.content


class Tree:
    """A reusable arranged UI object tree rendered in full."""

    def __init__(self, root, bounds):
        self.root = root
        self.bounds = bounds if isinstance(bounds, Rect) else Rect(*bounds)
        self.layout = LayoutResult()
        self.root.arrange(self.bounds, self.layout)

    def render(self, renderer, state=None):
        current = state if isinstance(state, StateStore) else StateStore((), state)
        if state is not None:
            self.root.update(current, initialize=True)
            self.layout = LayoutResult()
            self.root.arrange(self.bounds, self.layout)
        return self.root.render(renderer, current, self.layout)

    def rect(self, key):
        return self.layout[key]

    def node(self, key):
        return self.layout.node(key)


class PageDiscoveryContract:
    """Framework-owned AST prefilter contract for declarative page modules."""

    __slots__ = ("strategy", "factory_names", "module_names")

    def __init__(self, strategy, factory_names, module_names=()):
        self.strategy = str(strategy)
        self.factory_names = tuple(str(value) for value in factory_names)
        self.module_names = tuple(str(value) for value in module_names)

    def as_dict(self):
        return {
            "strategy": self.strategy,
            "factory_names": list(self.factory_names),
            "module_names": list(self.module_names),
        }


PAGE_DISCOVERY_CONTRACT = PageDiscoveryContract(
    "core.module_page_instances", ("PageTree", "DeclarativePage"),
    ("ui", "ui.layout"),
)


class PageMetadataSourceContract:
    """Framework-owned source grammar for editable page metadata."""

    __slots__ = ("strategy", "fields")

    def __init__(self, strategy, fields):
        self.strategy = str(strategy)
        self.fields = tuple(str(value) for value in fields)

    def as_dict(self):
        return {"strategy": self.strategy, "fields": list(self.fields)}


PAGE_METADATA_SOURCE_CONTRACT = PageMetadataSourceContract(
    "core.module_page_assignments", ("title", "show_back"))


class PageBoundsSourceContract:
    """Framework-owned source grammar for an editable page bounds literal.

    Only a direct ``Rect`` call with integer literal fields, passed as the
    declared positional or keyword page argument, is editable. A name,
    helper call or computed value keeps page bounds read-only.
    """

    __slots__ = ("strategy", "argument", "position", "callable", "fields")

    def __init__(self, strategy, argument, position, callable_name, fields):
        self.strategy = str(strategy)
        self.argument = str(argument)
        self.position = int(position)
        self.callable = str(callable_name)
        self.fields = tuple(str(value) for value in fields)

    def as_dict(self):
        return {"strategy": self.strategy, "argument": self.argument,
                "position": self.position, "callable": self.callable,
                "fields": list(self.fields)}


PAGE_BOUNDS_SOURCE_CONTRACT = PageBoundsSourceContract(
    "core.page_bounds_literal", "bounds", 1, "Rect",
    ("x", "y", "width", "height"))


class PageDeclarationsSourceContract:
    """Framework-owned grammar for adding typed declarations to a page module.

    New ``StateKey`` and ``CommandKey`` members are appended to classes that
    are defined directly at module level in the page module. New semantic
    actions are module-level assignments placed before the page construction.
    State members and actions are registered by appending to a literal tuple
    or list passed as the declared page keyword, or by adding that keyword.
    Imported key classes and computed registrations stay read-only.
    """

    __slots__ = ("strategy", "state_base", "command_base",
                 "state_schema_argument", "actions_argument", "supported_forms")

    def __init__(self, strategy, state_base, command_base,
                 state_schema_argument, actions_argument, supported_forms):
        self.strategy = str(strategy)
        self.state_base = str(state_base)
        self.command_base = str(command_base)
        self.state_schema_argument = str(state_schema_argument)
        self.actions_argument = str(actions_argument)
        self.supported_forms = tuple(str(value) for value in supported_forms)

    def as_dict(self):
        return {"strategy": self.strategy, "state_base": self.state_base,
                "command_base": self.command_base,
                "state_schema_argument": self.state_schema_argument,
                "actions_argument": self.actions_argument,
                "supported_forms": list(self.supported_forms)}


PAGE_DECLARATIONS_SOURCE_CONTRACT = PageDeclarationsSourceContract(
    "core.page_declarations", "StateKey", "CommandKey", "state_schema",
    "actions", ("module_key_classes", "module_action_assignments",
                "literal_registration"))


class ActionReferenceSourceContract:
    """Framework-owned grammar for naming a registered page action.

    A page action is referenceable when the page call registers it as a bare
    name in a literal ``actions`` tuple or list, and the page module binds
    that name exactly once, at module level, with a direct single-name
    assignment. A semantic-action field may then be written as that name; the
    field holds the same action object, so later edits of the declaration
    reach every use. The name is usable only below its assignment in the same
    module; other modules and computed registrations stay copy-only.
    """

    __slots__ = ("strategy", "actions_argument")

    def __init__(self, strategy, actions_argument):
        self.strategy = str(strategy)
        self.actions_argument = str(actions_argument)

    def as_dict(self):
        return {"strategy": self.strategy,
                "actions_argument": self.actions_argument}


ACTION_REFERENCE_SOURCE_CONTRACT = ActionReferenceSourceContract(
    "core.page_action_names", "actions")


def _validate_component_template_definitions(root, registered):
    """Reject one wire id referring to different template definitions."""
    definitions = {}
    instance_scopes = set()
    for template in tuple(registered or ()):
        definitions[_template_id(template.id)] = template
    for node in root.walk():
        template = getattr(node, "_component_template", None)
        if template is None:
            continue
        key = _template_id(template.id)
        existing = definitions.get(key)
        if existing is not None and existing is not template:
            raise ValueError(
                "ComponentTemplate identity %s refers to multiple definitions" %
                key)
        definitions[key] = template
        metadata = getattr(node, "_component_template_instance", {})
        if metadata.get("root") and metadata.get("instance_key") is not None:
            scope = metadata["instance_scope"]
            if scope in instance_scopes:
                raise ValueError("Duplicate ComponentTemplate instance_key: %r" %
                                 metadata["instance_key"])
            instance_scopes.add(scope)


class DeclarativePage(Tree):
    """An arranged page that discovers and redraws dirty subtrees."""

    discovery_contract = PAGE_DISCOVERY_CONTRACT
    metadata_source_contract = PAGE_METADATA_SOURCE_CONTRACT
    bounds_source_contract = PAGE_BOUNDS_SOURCE_CONTRACT
    declarations_source_contract = PAGE_DECLARATIONS_SOURCE_CONTRACT
    action_reference_contract = ACTION_REFERENCE_SOURCE_CONTRACT

    # The frame builder while it builds the page's frames: it built the last
    # one, and page.layout is the layout it published. The live path drops
    # it whenever it arranges; a full draw enters it again.
    _builder = None
    # (behavior_epoch(), _structure) when a build last needed Python; the page
    # draws on the live path while both hold.
    _build_refused = None

    def __init__(self, content, bounds, state=None, page_id=None,
                 state_schema=(), actions=(), styles=None,
                 component_templates=()):
        if not isinstance(page_id, PageKey):
            raise TypeError("DeclarativePage page_id must be a PageKey member")
        self._source = _capture_construction(
            self, names=("Page", "PageTree", "DeclarativePage"))
        self.page_key = page_id
        self.page_id = serialize_key(page_id)
        if styles is not None:
            from .styles import StyleSheet
            if not isinstance(styles, StyleSheet):
                raise TypeError("DeclarativePage styles must be a StyleSheet")
        self.styles = styles
        component_templates = tuple(component_templates or ())
        if not all(isinstance(value, ComponentTemplate)
                   for value in component_templates):
            raise TypeError(
                "DeclarativePage component_templates must contain ComponentTemplate values")
        template_ids = [_template_id(value.id) for value in component_templates]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("ComponentTemplate page identities must be unique")
        self.component_templates = component_templates
        self.state_schema = page_state_keys(content, state_schema)
        self.state = StateStore(self.state_schema, state)
        if state is not None:
            content.update(self.state, initialize=True)
        _validate_component_template_definitions(
            content, self.component_templates)
        self._styled_with = None
        if self.styles is not None:
            self._apply_styles(content)
        super().__init__(content, bounds)
        self._declared_actions = tuple(actions or ())
        self.actions = ActionTable()
        self._dependencies = None
        self._refresh_actions()
        self.initialized = state is not None
        self._layout_prepared = False
        self._layout_bounds = self.bounds
        self._layout_metrics_key = get_font_metrics().cache_key
        if self.initialized:
            self.root.clear_dirty()

    def __getstate__(self):
        # A copy builds its own frames: the frame builder holds the frames this
        # page built, and the copy's next full draw enters one of its own.
        state = dict(self.__dict__)
        state.pop("_builder", None)
        return state

    def _refresh_actions(self):
        # The table refuses a wire id shared by two different actions.
        actions = ActionTable()
        for action in self._declared_actions:
            actions.add(action_wire_id(validate_action(action)), action)
        for wire_id, action in collect_actions(self.root).items():
            actions.add(wire_id, action)
        self.actions = actions
        self._structure = _PageStructure(self.root)
        self._dependencies = _dependency_index(self._structure, self._dependencies)
        self.root._actions_dirty = False

    def initial_state(self):
        return StateStore(self.state_schema)

    def resolve_action(self, wire_id):
        return self.actions.get(str(wire_id))

    def action_metadata(self):
        from .actions import action_metadata
        return tuple(action_metadata(action)
                     for _wire, action in sorted(self.actions.items()))

    def state_metadata(self):
        return self.state.metadata()

    def _fresh_state(self, values=None):
        return StateStore(self.state_schema, values)

    def prepare_layout(self):
        """Arrange the declared state before the first renderer-backed draw."""
        if self.initialized:
            return
        self.root.update(self.state, initialize=True)
        if self.styles is not None:
            self._apply_styles(self.root)
        self.layout = LayoutResult()
        self._builder = None
        self.root.arrange(self.bounds, self.layout)
        self.root.clear_dirty()
        self.state.clear_changes()
        self._layout_bounds = self.bounds
        self._layout_metrics_key = get_font_metrics().cache_key
        self._layout_prepared = True

    def draw(self, renderer, state=None, reuse_layout=False, refs=None,
             reuse_styles=False, render_from=None):
        """Draw a full surface; refs and static styles may reuse a clean tree.

        render_from limits painting to retained foreground nodes; callers own
        the unchanged pixels beneath them and must redraw fully after damage.
        Callers supplying refs must include every node affected by the new
        state. The style pass runs when its inputs changed (see update());
        reuse_styles skips it for a clean reused tree. Direct edits to
        layout_options or styled values require root.invalidate_layout()
        before drawing.
        """
        clean = self.root._dirty == Dirty.CLEAN and not self.state.changed_keys
        reuse_layout = bool(
            reuse_layout and (self.initialized or self._layout_prepared)
            and self._layout_bounds == self.bounds
            and self._layout_metrics_key == get_font_metrics().cache_key)
        reuse_cached = reuse_layout and clean
        self.state = self._fresh_state(state)
        if reuse_cached and refs is not None:
            for ref in refs:
                node = self.node(ref)
                node.update(self._paint_state(node))
        else:
            self.commit(everything=True)
            self.root.update(self.state, initialize=not reuse_layout)
        if (self.styles is not None and not (reuse_cached and reuse_styles)
                and not self._styles_current()):
            self._apply_styles(self.root)
        # The structure (actions, render order, dependency index) is rebuilt
        # only when it changed: membership, actions and refs raise the root's
        # _actions_dirty, and stale dependencies refresh in the next update.
        return self._render_full(
            renderer,
            arrange=not reuse_layout or self.root._dirty >= Dirty.LAYOUT,
            refresh_actions=self.root._actions_dirty,
            render_from=render_from)

    def _render_full(self, renderer, arrange, refresh_actions, render_from=None,
                     prepared_layout=None):
        if refresh_actions:
            self._refresh_actions()
        set_page_identity = getattr(renderer, "set_semantic_page", None)
        if set_page_identity is not None:
            set_page_identity(self.page_id)
        commands = self._build_full(renderer, arrange, render_from)
        if commands is None:
            if arrange:
                self.layout = prepared_layout if prepared_layout is not None else LayoutResult()
                self._builder = None
                if prepared_layout is None:
                    self.root.arrange(self.bounds, self.layout)
            if render_from is None:
                commands = self.root.render(renderer, self.state, self.layout)
            else:
                commands = CommandList()
                for node in render_from:
                    commands.extend(node.render_dirty(
                        renderer, self._paint_state(node), self.layout))
        if arrange:
            self._layout_bounds = self.bounds
            self._layout_metrics_key = get_font_metrics().cache_key
        self.root.clear_dirty()
        self.initialized = True
        self.state.clear_changes()
        return commands

    def _build_full(self, renderer, arrange, render_from):
        """Build a full draw on the page's frame builder.

        Returns None when the live path draws it instead: the draw that
        initializes the page (opening a page whose frames need Python then
        costs no refused build; its next full draw enters the builder), a
        build needed Python since the structure and class behaviours last
        changed, the renderer may not draw, or this build needs Python. A
        frame builder entering without arranging continues from the page's
        layout.
        """
        if not self.initialized or self._build_refused == (behavior_epoch(), self._structure):
            return None
        builder = self._builder if self._builder is not None else FrameBuilder()
        commands = self._build(renderer, builder, lambda: builder.draw(
            self.root, renderer, self.state, self.bounds, arrange=arrange, render_from=render_from,
            layout=self.layout))
        if commands is not None:
            self.layout = builder.layout
        return commands

    def _build_update(self, renderer, frame):
        """Build an update frame, the frame builder's method of that name
        (repaint or replay), on the page's frame builder. Returns None when
        the live path renders it instead: the page is not building its
        frames, the renderer may not draw, or the build needs Python."""
        builder = self._builder
        if builder is None:
            return None
        return self._build(renderer, builder, lambda: getattr(builder, frame)(self.root, renderer, self.state))

    def _build(self, renderer, builder, build):
        """Run build() on builder, which the page then builds its frames on.
        Returns None, leaving the builder, when the renderer may not draw or
        the build needs Python. The live paint prepares the renderer's
        surface at its first button; a build prepares it before."""
        prepare = getattr(renderer, "_prepare_surface", None)
        if prepare is not None and not prepare():
            self._builder = None
            return None
        try:
            commands = build()
        except RuntimeError:
            self._refuse_build()
            return None
        except BaseException:
            # A failed build kept no layout.
            self._builder = None
            raise
        self._builder = builder
        return commands

    def _refuse_build(self):
        """Remember that building the page's frames needs Python, until its
        structure or a node class's behaviour changes, and leave them to the
        live path, which continues from the layout the builder published."""
        self._builder = None
        self._build_refused = (behavior_epoch(), self._structure)

    def update(self, renderer, state=None):
        if state is not None:
            self.state.update(state)
        if not self.initialized:
            return self.draw(renderer, self.state)
        # New font metrics re-measure text that no state key changed.
        metrics_current = self._layout_metrics_key == get_font_metrics().cache_key
        if not metrics_current:
            self.root.invalidate_layout()
        self.commit()
        self._update_nodes((self.root,), metrics_current)
        if self.styles is not None and not self._styles_current():
            self._apply_styles(self.root)
        return self._render_updates(renderer)

    def commit(self, everything=False):
        """Run the Python work of the state's changes before the update pass.

        Evaluates the derived bindings of the nodes reading a changed key
        (with everything, of every indexed node) that the state keeps no
        result of; a node under a branch that stays hidden is left alone, as
        the pass leaves it. The state keeps the plain results, so the
        engine's update pass and paint read them without calling Python.
        Nodes the index does not cover (ListView items, nodes whose
        dependencies are stale) and content a branch shows in this update
        still evaluate theirs in the pass.
        """
        self._dependencies.commit(self.state, _RESOLVER, everything)

    def _apply_styles(self, root):
        """Run the complete style pass and remember the rules it applied."""
        self.styles.apply(root)
        self._styled_with = (self.styles, self.styles.rules_snapshot())

    def _styles_current(self):
        """Return whether an update can keep the previous style pass.

        A pass depends on the rules and on the tree's structure, style ids and
        explicit values. Framework modifiers and structural changes invalidate
        layout, so an update that only repaints bound values cannot change the
        result. Rules are mutable data and are re-observed against the snapshot
        of the last successful pass. Writes that bypass framework invalidation
        take effect at the next layout invalidation or rule change.
        """
        applied = self._styled_with
        return (applied is not None and self.root._dirty < Dirty.LAYOUT
                and applied[0] is self.styles and self.styles.rules_match(applied[1]))

    def update_refs(self, renderer, state, refs):
        """Update known state-bound nodes without traversing unrelated content.

        The caller must name every node affected by the supplied state values.
        Dynamic styles and structural changes use the regular update path.
        Dependents outside the named subtrees catch up at the next update. The
        engine runs the whole frame.
        """
        return _update_refs(self, renderer, state, refs, get_font_metrics().cache_key)

    def _update_nodes(self, nodes, prune):
        """Update nodes, skipping clean subtrees no changed key requested."""
        update_nodes(self, nodes, prune)

    def _render_updates(self, renderer):
        """Render the updated nodes: a relayout through the engine's partial
        path when it can serve, else a full draw; otherwise a repaint of the
        dirty roots. The frame builder renders them while it builds the
        page's frames. Changed actions take _refresh_input_updates."""
        return render_updates(self, renderer, get_font_metrics().cache_key)

    def _refresh_input_updates(self, renderer):
        """Render an update whose actions changed: replay the input over a
        repaint of the dirty roots when the renderer can and every control
        replays its own, else draw the page in full. The frame builder
        builds the repaint and the replay while it builds the page's
        frames."""
        refreshed = self.root._dirty < Dirty.LAYOUT and hasattr(renderer, "rebuild_page_input")
        if refreshed:
            # The new structure knows whether every node holding actions
            # replays them (replayable).
            self._refresh_actions()
        if not refreshed or not self._structure.replayable:
            commands = CommandList(renderer.redraw_page())
            commands.extend(self._render_full(renderer, arrange=True, refresh_actions=not refreshed))
            return commands
        commands = CommandList()
        try:
            built = self._build_update(renderer, "repaint")
            if built is not None:
                commands.extend(built)
            else:
                for root in self._dirty_roots():
                    commands.extend(self._background_repair(renderer, root))
                    commands.extend(
                        root.render_dirty(
                            renderer, self._paint_state(root), self.layout))
            # Typer removes hitboxes per layer. Replay input in paint order
            # while keeping unchanged pixels and geometry intact.
            with renderer.rebuild_page_input() as input_commands:
                if self._build_update(renderer, "replay") is None:
                    self.root.render_interactions(renderer, self.state, self.layout)
            commands.extend(input_commands)
        except Exception:
            # A partially prepared input layer needs a complete retry.
            self.root.invalidate_layout()
            raise
        self.root.clear_dirty()
        self.state.clear_changes()
        return commands

    def invalidate(self, key, dirty=Dirty.PAINT):
        self.node(key).invalidate(dirty)

    def _dirty_roots(self):
        return dirty_roots(self)

    def _paint_state(self, node):
        return paint_state(self, node)

    def _background_repair(self, renderer, root):
        """Restore pixels below a transparent dirty composition."""
        return background_repair(self, renderer, root)


PageTree = DeclarativePage


def _command_list(value):
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        return [value]
    return list(value)
