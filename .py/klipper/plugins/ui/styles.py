## Typed declarative style support for Feather UI.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from enum import Enum

from .engine import StyleApplier, style_rules_match
from .layout import Insets, LAYOUT_SCHEMA, Node, ParamRef
from .properties import inheritable_style_properties
from .source import (
    capture_construction, construction_metadata, style_property_provenance,
)


def _style_id(value):
    if not isinstance(value, Enum):
        raise TypeError("Style identities must be Enum members")
    return value.value


def _component_specs(node_type):
    return dict((spec.name, spec) for spec in getattr(node_type, "property_schema", ()))


def _layout_specs():
    return dict((spec.name, spec) for spec in LAYOUT_SCHEMA)


def _inheritable_specs():
    # Context-style semantics are registered centrally and are therefore
    # independent of the order in which component modules are imported.
    return inheritable_style_properties()


def _copy_rule_value(value):
    # Rule values are portable data: copy their mutable containers so a later
    # in-place edit is observed as a change. Other values are compared as-is.
    if type(value) is dict:
        return dict((key, _copy_rule_value(item)) for key, item in value.items())
    if type(value) is list:
        return [_copy_rule_value(item) for item in value]
    if type(value) is tuple:
        return tuple(_copy_rule_value(item) for item in value)
    return value


# The engine's stylesheet pass, given the framework's types, Node's size
# normalization and the styleable layout fields in schema order.
_APPLIER = StyleApplier(Enum, Insets, ParamRef, Node._size_value,
                        tuple(spec.name for spec in LAYOUT_SCHEMA if spec.styleable))


class Style:
    """One typed style declaration with optional single inheritance."""

    def __init__(self, style_id, target=None, based_on=None, **properties):
        _style_id(style_id)
        if target is not None and (not isinstance(target, type)
                                   or not issubclass(target, Node)):
            raise TypeError("Style target must be a Node type or None")
        if based_on is not None:
            _style_id(based_on)
        self.id = style_id
        self.target = target
        self.based_on = based_on
        self.properties = dict(properties)
        self._source = capture_construction(self, names=("Style",))


class StyleSheet:
    """Validated collection of typed styles used by a page."""

    def __init__(self, *styles):
        if not all(isinstance(value, Style) for value in styles):
            raise TypeError("StyleSheet accepts only Style values")
        ids = [_style_id(value.id) for value in styles]
        if len(ids) != len(set(ids)):
            raise ValueError("Style wire identities must be unique")
        self.styles = tuple(styles)
        self._by_id = dict((_style_id(value.id), value) for value in styles)
        self._source = capture_construction(self, names=("StyleSheet",))
        self._validate()

    def _validate(self):
        layout = _layout_specs()
        inheritable = _inheritable_specs()
        for style in self.styles:
            if (style.based_on is not None
                    and _style_id(style.based_on) not in self._by_id):
                raise ValueError("Unknown base style: %s" % style.based_on)
            component = _component_specs(style.target) if style.target else {}
            for name, value in style.properties.items():
                if style.target is None:
                    spec = inheritable.get(name)
                    if spec is None:
                        raise ValueError(
                            "Context style property %s is not inheritable" % name)
                    spec.validate(value)
                    continue
                component_spec = component.get(name)
                layout_spec = layout.get(name)
                component_ok = component_spec is not None and component_spec.styleable
                layout_ok = layout_spec is not None and layout_spec.styleable
                if component_ok and layout_ok:
                    raise ValueError("Ambiguous style property: %s" % name)
                if component_ok:
                    component_spec.validate(value)
                elif layout_ok:
                    layout_spec.validate(value)
                else:
                    raise ValueError(
                        "%s is not styleable on %s" % (name, style.target.__name__))

        visiting = set()
        visited = set()

        def visit(style):
            if style.id in visited:
                return
            if style.id in visiting:
                raise ValueError("Style inheritance cycle at %s" % style.id)
            visiting.add(style.id)
            if style.based_on is not None:
                base = self._by_id[_style_id(style.based_on)]
                if style.target is None and base.target is not None:
                    raise ValueError("A context style cannot derive from a targeted style")
                if (style.target is not None and base.target is not None
                        and not issubclass(style.target, base.target)):
                    raise ValueError("Derived style target is incompatible with its base")
                visit(base)
            visiting.remove(style.id)
            visited.add(style.id)

        for style in self.styles:
            visit(style)

    def rules_snapshot(self):
        """Copy the mutable rule state that one style pass consumes."""
        return tuple((style, style.target, style.based_on, _copy_rule_value(style.properties))
                     for style in self.styles)

    def rules_match(self, snapshot):
        """Re-observe the live rules against a snapshot, including in-place value edits."""
        return style_rules_match(snapshot, self.styles)

    def get(self, style_id):
        key = _style_id(style_id)
        try:
            return self._by_id[key]
        except KeyError:
            raise ValueError("Unknown style: %s" % key) from None

    @staticmethod
    def _origin(kind, style=None, property_name=None, inherited_from=None):
        result = {"kind": kind}
        if style is not None:
            result["style"] = _style_id(style.id)
            result["style_source"] = construction_metadata(style)
            if property_name is not None:
                result["style_property_source"] = style_property_provenance(
                    style, property_name, style.properties.get(property_name))
        if inherited_from is not None:
            result["inherited_from"] = inherited_from
        return result

    def apply(self, root):
        """Compute and apply the stylesheet using CSS-free RFC precedence."""
        if not isinstance(root, Node):
            raise TypeError("StyleSheet root must be a Node")
        # The engine resolves styles and origins within this pass only. A later
        # pass still observes mutable styles and authoring provenance.
        _APPLIER.apply(self, root, _inheritable_specs())

    def as_dict(self):
        return {
            "source": construction_metadata(self),
            "styles": [self.style_metadata(value) for value in self.styles],
        }

    def style_metadata(self, style):
        component = _component_specs(style.target) if style.target else {}
        layout = _layout_specs()
        component_values = {}
        layout_values = {}
        context_values = {}
        for name, value in style.properties.items():
            if name in component and component[name].styleable:
                component_values[name] = value
            elif name in layout and layout[name].styleable and style.target is not None:
                layout_values[name] = value
            else:
                context_values[name] = value
        return {
            "id": _style_id(style.id),
            "symbol": "%s.%s" % (style.id.__class__.__name__, style.id.name),
            "qualified_symbol": "%s.%s.%s" % (
                style.id.__class__.__module__, style.id.__class__.__name__, style.id.name),
            "preview_only": bool(getattr(style, "_designer_draft", False)),
            "target": None if style.target is None else style.target.__name__,
            "based_on": None if style.based_on is None else _style_id(style.based_on),
            "component_properties": component_values,
            "layout_properties": layout_values,
            "context_properties": context_values,
            "property_sources": dict((
                name, style_property_provenance(style, name, value))
                for name, value in style.properties.items()),
            "source": construction_metadata(style),
        }
