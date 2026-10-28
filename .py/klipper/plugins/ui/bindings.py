## Typed state declarations and bindings for the Feather UI framework.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import copy
import inspect
from enum import Enum

from .engine import BindingResolver, StateStoreBase, portable
from .identity import StateKey, serialize_key
from .theme import ThemeColor, ThemeRole


_UNAVAILABLE = object()


class StateDeclarationContract:
    """Framework-owned source grammar for one ``StateKey`` member."""

    __slots__ = (
        "strategy", "factory_names", "positional_fields", "editable_fields",
        "field_codecs", "portable_types", "supported_forms",
    )

    def __init__(self, strategy, factory_names, positional_fields,
                 editable_fields, field_codecs, portable_types,
                 supported_forms=("direct_enum_assignment",)):
        self.strategy = str(strategy)
        self.factory_names = tuple(str(value) for value in factory_names)
        self.positional_fields = tuple(str(value) for value in positional_fields)
        self.editable_fields = tuple(str(value) for value in editable_fields)
        self.field_codecs = dict((str(name), str(codec))
                                 for name, codec in field_codecs.items())
        self.portable_types = tuple(portable_types)
        self.supported_forms = tuple(str(value) for value in supported_forms)

    def as_dict(self):
        return {
            "strategy": self.strategy,
            "factory_names": list(self.factory_names),
            "positional_fields": list(self.positional_fields),
            "editable_fields": list(self.editable_fields),
            "field_codecs": dict(self.field_codecs),
            "portable_types": [_type_name(value)
                               for value in self.portable_types],
            "supported_forms": list(self.supported_forms),
        }


class BindingExpressionContract:
    """Framework-owned grammar for authoring a derived expression."""

    __slots__ = (
        "strategy", "factory_names", "callable_forms", "allowed_ast_nodes",
        "allowed_globals", "portable_default_types", "sharing",
    )

    def __init__(self, strategy, factory_names, callable_forms,
                 allowed_ast_nodes, allowed_globals=(),
                 portable_default_types=(), sharing="catalog_uses"):
        self.strategy = str(strategy)
        self.factory_names = tuple(str(value) for value in factory_names)
        self.callable_forms = tuple(str(value) for value in callable_forms)
        self.allowed_ast_nodes = tuple(str(value) for value in allowed_ast_nodes)
        self.allowed_globals = tuple(str(value) for value in allowed_globals)
        self.portable_default_types = tuple(portable_default_types)
        self.sharing = str(sharing)

    def as_dict(self):
        return {
            "strategy": self.strategy,
            "factory_names": list(self.factory_names),
            "callable_forms": list(self.callable_forms),
            "allowed_ast_nodes": list(self.allowed_ast_nodes),
            "allowed_globals": list(self.allowed_globals),
            "portable_default_types": [
                _type_name(value) for value in self.portable_default_types],
            "sharing": self.sharing,
        }


_STATE_FIELDS = (
    "value_type", "default", "minimum", "maximum", "choices", "mutable",
    "unit", "category", "simulation_role", "simulation_home",
)
STATE_DECLARATION_CONTRACT = StateDeclarationContract(
    "core.state_enum_member", ("state",), _STATE_FIELDS, _STATE_FIELDS[1:],
    dict((name, "python_literal") for name in _STATE_FIELDS),
    (bool, int, float, str, list),
)
BINDING_EXPRESSION_CONTRACT = BindingExpressionContract(
    "core.python_expression", ("derived",),
    ("inline_lambda", "same_module_single_return"),
    (
        "Expression", "BoolOp", "BinOp", "UnaryOp", "IfExp", "Compare",
        "Name", "Load", "Constant", "Attribute", "Subscript", "Slice",
        "Tuple", "List", "Dict",
        "Set", "JoinedStr", "FormattedValue", "And", "Or", "Not", "UAdd",
        "USub", "Add", "Sub", "Mult", "Div", "FloorDiv", "Mod", "Pow",
        "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot", "In",
        "NotIn",
    ),
    allowed_globals=("ThemeColor", "ThemeRole"),
    portable_default_types=(
        type(None), str, int, float, bool, tuple, list, dict, Enum),
)


def _capture_binding(binding, names=()):
    return None


def _install_source_hook(capture_binding):
    """Install the optional neutral binding provenance hook."""
    global _capture_binding
    _capture_binding = capture_binding


def _type_name(value_type):
    return "%s.%s" % (
        getattr(value_type, "__module__", "builtins"),
        getattr(value_type, "__qualname__", getattr(value_type, "__name__", str(value_type))),
    )


def _json_value(value):
    if isinstance(value, Enum):
        try:
            return serialize_key(value)
        except TypeError:
            return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return dict((str(_json_value(key)), _json_value(item))
                    for key, item in value.items())
    return value


_NOT_PLAIN = object()


def _plain_copy(value, seen):
    kind = type(value)
    if value is None or kind in (str, int, float, bool):
        return value
    if kind not in (list, tuple, dict) or id(value) in seen:
        return _NOT_PLAIN
    seen.add(id(value))
    if kind is dict:
        result = {}
        for key, item in value.items():
            copied = _plain_copy(item, seen)
            if type(key) is not str or copied is _NOT_PLAIN:
                return _NOT_PLAIN
            result[key] = copied
        return result
    result = []
    for item in value:
        copied = _plain_copy(item, seen)
        if copied is _NOT_PLAIN:
            return _NOT_PLAIN
        result.append(copied)
    if kind is list:
        return result
    # Like deepcopy, a tuple whose items copy to themselves is returned as is.
    return value if all(a is b for a, b in zip(result, value)) else tuple(result)


def _copy_value(value):
    """Return an isolated copy of a state value, exactly as deepcopy would.

    Trees of exact built-in JSON-like types are copied directly, skipping
    deepcopy's memo and dispatch. Shared or cyclic containers, whose aliases
    deepcopy preserves, and every other type keep deepcopy.
    """
    copied = _plain_copy(value, set())
    return copy.deepcopy(value) if copied is _NOT_PLAIN else copied


def _validate_portable_collection(value, path="state"):
    # Valid data is checked without building element paths for diagnostics.
    if portable(value):
        return
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, (list, tuple)):
        for index, item_value in enumerate(value):
            _validate_portable_collection(
                item_value, "%s[%d]" % (path, index))
        return
    if isinstance(value, dict):
        for key, item_value in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    "%s mapping keys must be strings" % path)
            _validate_portable_collection(
                item_value, "%s.%s" % (path, key))
        return
    raise TypeError(
        "%s contains non-portable %s" % (path, type(value).__name__))


class StateSpec:
    """Metadata and validation for one application-defined state key."""

    __slots__ = (
        "value_type", "default", "minimum", "maximum", "choices",
        "mutable", "unit", "category", "simulation_role", "simulation_home",
        "declaration_source_contract",
    )

    def __init__(self, value_type, default=_UNAVAILABLE, minimum=None,
                 maximum=None, choices=None, mutable=True, unit=None,
                 category=None, simulation_role=None, simulation_home=None):
        if not isinstance(value_type, type):
            raise TypeError("state value_type must be a type")
        self.value_type = value_type
        self.default = default
        self.minimum = minimum
        self.maximum = maximum
        self.choices = None if choices is None else tuple(choices)
        self.mutable = bool(mutable)
        self.unit = None if unit is None else str(unit)
        self.category = None if category is None else str(category)
        self.simulation_role = (None if simulation_role is None
                                else str(simulation_role).strip())
        if self.simulation_role == "":
            raise ValueError("simulation_role must not be empty")
        self.simulation_home = simulation_home
        self.declaration_source_contract = None
        if self.simulation_home is not None and self.simulation_role is None:
            raise ValueError("simulation_home requires simulation_role")
        if self.simulation_home is not None:
            self.validate(self.simulation_home)
        if default is not _UNAVAILABLE:
            self.validate(default)

    @property
    def available(self):
        return self.default is not _UNAVAILABLE

    def default_value(self):
        if not self.available:
            raise ValueError("State value has no default")
        return _copy_value(self.default)

    def validate(self, value):
        if value is None:
            if self.default is None:
                return value
            raise TypeError("State value cannot be None")
        if self.value_type is float and isinstance(value, int):
            value = float(value)
        elif self.value_type is int and isinstance(value, bool):
            raise TypeError("Boolean is not a valid integer state value")
        elif not isinstance(value, self.value_type):
            raise TypeError("Expected %s state value, got %s" % (
                self.value_type.__name__, type(value).__name__))
        if self.value_type is list:
            _validate_portable_collection(value)
        if self.minimum is not None and value < self.minimum:
            raise ValueError("State value is below minimum %s" % self.minimum)
        if self.maximum is not None and value > self.maximum:
            raise ValueError("State value is above maximum %s" % self.maximum)
        if self.choices is not None and value not in self.choices:
            raise ValueError("State value is not one of the allowed choices")
        return value

    def as_dict(self):
        return {
            "type": _type_name(self.value_type),
            "nullable": self.available and self.default is None,
            "default_available": self.available,
            "default": None if not self.available else _json_value(self.default),
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": None if self.choices is None else _json_value(self.choices),
            "mutable": self.mutable,
            "unit": self.unit,
            "category": self.category,
            "simulation_role": self.simulation_role,
            "simulation_home": _json_value(self.simulation_home),
            "declaration_source_contract": (
                None if self.declaration_source_contract is None
                else self.declaration_source_contract.as_dict()),
        }


def state(value_type, default=_UNAVAILABLE, minimum=None, maximum=None,
          choices=None, mutable=True, unit=None, category=None,
          simulation_role=None, simulation_home=None):
    """Declare metadata for a ``StateKey`` enum member."""
    spec = StateSpec(
        value_type, default=default, minimum=minimum, maximum=maximum,
        choices=choices, mutable=mutable, unit=unit, category=category,
        simulation_role=simulation_role, simulation_home=simulation_home)
    spec.declaration_source_contract = STATE_DECLARATION_CONTRACT
    return spec


def state_spec(key):
    if not isinstance(key, StateKey):
        raise TypeError("State binding key must be a StateKey member")
    spec = key._value_  # .value is a Python level descriptor
    if not isinstance(spec, StateSpec):
        raise TypeError("StateKey members must be declared with state(...)")
    return spec


class StateStore(StateStoreBase):
    """Validated state values addressed only by typed ``StateKey`` members.

    The engine keeps the values, the changed keys and iteration order;
    reading, membership and ``update`` run there. This class adds the
    framework's error for undeclared keys, value copies and the cold views.
    """

    __slots__ = ()

    def __init__(self, keys=(), values=None):
        schema = _unique_keys(keys)
        # None: the engine stores a copy of each available default.
        self._open(schema, schema.specs, None, _UNAVAILABLE)
        if values is not None:
            self.update(values)

    @staticmethod
    def _unknown_key(key):
        if not isinstance(key, StateKey):
            raise TypeError("State values must be addressed by StateKey members")
        raise KeyError("State key is not declared by this page: %s" % key)

    _copy_value = staticmethod(_copy_value)

    def copy(self):
        return StateStore(self.schema, self._entries())

    def __deepcopy__(self, memo):
        copied = StateStore(self.schema)
        copied._restore(copy.deepcopy(self._entries(), memo), self.changed_keys)
        return copied

    def keys(self):
        return dict.fromkeys(self).keys()

    def values(self):
        return self.as_dict().values()

    def items(self):
        return self.as_dict().items()

    def as_dict(self, serialized=False):
        if serialized:
            return dict((serialize_key(key), _json_value(value))
                        for key, value in self._entries())
        return copy.deepcopy(dict(self._entries()))

    def metadata(self):
        result = []
        for key in self.schema:
            item = state_spec(key).as_dict()
            item.update({
                "key": serialize_key(key),
                "name": key.name,
                "value_available": key in self,
                "value": _json_value(self._owned(key)) if key in self else None,
            })
            result.append(item)
        return result


class _Schema(tuple):
    """Validated unique state keys; ``specs`` holds their StateSpecs in order."""


def _unique_keys(keys):
    # Pages rebuild their store on every full draw; a validated schema is reused as is.
    if type(keys) is _Schema:
        return keys
    result = []
    seen = set()
    for value in tuple(keys or ()):
        if isinstance(value, type) and issubclass(value, StateKey):
            values = tuple(value)
        else:
            values = (value,)
        for key in values:
            spec = state_spec(key)
            if key not in seen:
                result.append((key, spec))
                seen.add(key)
    schema = _Schema(key for key, _spec in result)
    schema.specs = tuple(spec for _key, spec in result)
    return schema


class Binding:
    """Base class for values resolved from an explicit typed state schema."""

    __slots__ = ()

    @property
    def keys(self):
        raise NotImplementedError

    def resolve(self, store):
        raise NotImplementedError

    def as_dict(self):
        raise NotImplementedError


class DirectBinding(Binding):
    __slots__ = ("key", "_source")

    def __init__(self, key):
        state_spec(key)
        self.key = key
        self._source = None

    @property
    def keys(self):
        return (self.key,)

    def resolve(self, store):
        return store[self.key]

    def as_dict(self):
        return {"kind": "direct", "key": serialize_key(self.key)}


class ItemBinding(Binding):
    """A value resolved from the current data-driven container item."""

    __slots__ = ("field", "_source")

    def __init__(self, field):
        if not isinstance(field, str) or not field:
            raise TypeError("item() field must be a non-empty string")
        self.field = field
        self._source = None

    @property
    def keys(self):
        return ()

    def resolve(self, store):
        current = getattr(store, "item", _UNAVAILABLE)
        if current is _UNAVAILABLE:
            raise RuntimeError(
                "item(%r) can only resolve inside a Template item scope" %
                self.field)
        if self.field not in current:
            raise KeyError("Template item has no field %r" % self.field)
        value = current[self.field]
        return value if value is None or type(value) in (str, int, float, bool) else _copy_value(value)

    def as_dict(self):
        return {"kind": "item", "field": self.field}


class ItemScope:
    """Read-only item context layered over an ordinary ``StateStore``."""

    __slots__ = ("state", "item")

    @staticmethod
    def _validate_scope(state, current):
        if not isinstance(state, StateStore):
            raise TypeError("ItemScope requires a StateStore")
        if not isinstance(current, dict):
            raise TypeError("Template item scope requires a mapping")

    def __init__(self, state, current):
        self._validate_scope(state, current)
        self.state = state
        self.item = copy.deepcopy(current)

    @classmethod
    def _from_snapshot(cls, state, current):
        """Adopt the already isolated collection item owned by ListView."""
        return _RESOLVER.item_scope(state, current)

    def __getitem__(self, key):
        return self.state[key]

    def __contains__(self, key):
        return key in self.state

    def __iter__(self):
        return iter(self.state)

    def __len__(self):
        return len(self.state)

    @property
    def schema(self):
        return self.state.schema


class DerivedBinding(Binding):
    """Pure function of its inputs, evaluated again only when an input changes."""

    __slots__ = ("function", "inputs", "_source", "expression_source_contract", "_cache")

    def __init__(self, function, inputs):
        if not callable(function):
            raise TypeError("derived() requires a callable")
        inputs = tuple(inputs)
        if not inputs or not all(isinstance(value, Binding) for value in inputs):
            raise TypeError("derived() inputs must be explicit bindings")
        _validate_callable_arity(function, len(inputs))
        self.function = function
        self.inputs = inputs
        self._source = None
        self.expression_source_contract = None
        self._cache = None

    @property
    def keys(self):
        result = []
        seen = set()
        for item in self.inputs:
            for key in item.keys:
                if key not in seen:
                    result.append(key)
                    seen.add(key)
        return tuple(result)

    def resolve(self, store):
        # The engine evaluates the function again only when an input changed;
        # plain scalar and enum inputs are remembered in _cache.
        return _RESOLVER.derived_value(self, store)

    def as_dict(self):
        return {
            "kind": "derived",
            "inputs": [item.as_dict() for item in self.inputs],
            "callable": getattr(self.function, "__name__", "<callable>"),
        }


def _validate_callable_arity(function, count):
    target = function
    code = getattr(target, "__code__", None)
    bound = getattr(target, "__self__", None) is not None
    if code is None:
        target = getattr(function, "__call__", None)
        code = getattr(target, "__code__", None)
        bound = target is not None
    if code is None:
        # Some extension callables do not expose a Python signature. Their
        # invocation remains the authoritative compatibility check.
        return

    positional = int(code.co_argcount) - int(bound)
    defaults = getattr(target, "__defaults__", None) or ()
    minimum = max(0, positional - len(defaults))
    maximum = None if code.co_flags & 0x04 else positional
    keyword_defaults = getattr(target, "__kwdefaults__", None) or {}
    required_keywords = max(
        0, int(getattr(code, "co_kwonlyargcount", 0))
        - len(keyword_defaults))
    if (required_keywords or count < minimum
            or (maximum is not None and count > maximum)):
        raise TypeError("derived() callable does not accept %d inputs" % count)


def bind(key):
    binding = DirectBinding(key)
    binding._source = _capture_binding(binding, names=("bind",))
    return binding


def item(field):
    binding = ItemBinding(field)
    binding._source = _capture_binding(binding, names=("item",))
    return binding


def derived(function, *inputs):
    binding = DerivedBinding(function, inputs)
    binding.expression_source_contract = BINDING_EXPRESSION_CONTRACT
    binding._source = _capture_binding(binding, names=("derived",))
    return binding


# The engine resolves values: literals as they are, bindings resolved (the
# framework's binding types without calling their methods), theme tokens as
# themselves and other enum members as their values. signature_value freezes a
# resolved value for Component's state signature, and item_scope makes
# ListView's item scopes.
_RESOLVER = BindingResolver(Binding, DirectBinding, ItemBinding, DerivedBinding,
                            StateStore, ItemScope, (ThemeColor, ThemeRole), serialize_key)
resolve = _RESOLVER.resolve
signature_value = _RESOLVER.signature_value
# binding_reads(value): (keys, derived), the state keys the bindings within
# value read (tuples, lists and dict values searched), each once, in the order
# found, and the derived bindings found outside other derived bindings.
binding_reads = _RESOLVER.binding_reads


def resolve_deep(value, store):
    value = resolve(value, store)
    if isinstance(value, Enum):
        try:
            return serialize_key(value)
        except TypeError:
            return value.value
    if isinstance(value, tuple):
        return tuple(resolve_deep(item, store) for item in value)
    if isinstance(value, list):
        return [resolve_deep(item, store) for item in value]
    if isinstance(value, dict):
        return dict((key, resolve_deep(item, store))
                    for key, item in value.items())
    return value


def _binding_resolution(binding, store):
    try:
        return {"resolved": _json_value(binding.resolve(store))}
    except Exception as error:
        return {"resolved": None, "error": str(error)}


def _callable_metadata(function):
    result = {
        "name": getattr(function, "__name__", "<callable>"),
        "qualname": getattr(function, "__qualname__", None),
        "module": getattr(function, "__module__", None),
    }
    code = getattr(function, "__code__", None)
    if code is not None:
        result["location"] = {
            "file": inspect.getsourcefile(function),
            "line": int(code.co_firstlineno),
        }
    return result


def _binding_source_metadata(binding):
    trace = getattr(binding, "_source", None)
    if not isinstance(trace, dict):
        return None
    anchor = trace.get("anchor")
    if not isinstance(anchor, dict):
        return None
    return {
        "file": anchor.get("file"),
        "relative_file": anchor.get("relative_file"),
        "range": anchor.get("range"),
        "expression": anchor.get("expression"),
        "kind": anchor.get("kind"),
        "callable": anchor.get("callable"),
        "fingerprint": anchor.get("fingerprint"),
        "context_fingerprint": anchor.get("context_fingerprint"),
        "file_fingerprint": anchor.get("file_fingerprint"),
        "call_chain": list(trace.get("chain", ())),
    }


def binding_metadata(binding, store):
    """Return resolved, recursive Designer metadata for one binding.

    The compact :meth:`Binding.as_dict` representation remains suitable for
    persistent/runtime contracts. This richer form is produced only during
    reflection, so normal printer rendering does not retain dependency trees.
    """
    if not isinstance(binding, Binding):
        raise TypeError("binding_metadata requires a Binding")
    keys = [serialize_key(key) for key in binding.keys]
    if isinstance(binding, DirectBinding):
        result = {
            "kind": "direct",
            "key": serialize_key(binding.key),
            "keys": keys,
            "direct_keys": keys,
            "transitive_keys": [],
            "source": _binding_source_metadata(binding),
        }
        result.update(_binding_resolution(binding, store))
        return result
    if isinstance(binding, ItemBinding):
        result = {
            "kind": "item",
            "field": binding.field,
            "keys": [],
            "direct_keys": [],
            "transitive_keys": [],
            "source": _binding_source_metadata(binding),
        }
        result.update(_binding_resolution(binding, store))
        return result
    if isinstance(binding, DerivedBinding):
        inputs = [binding_metadata(item, store) for item in binding.inputs]
        direct = []
        transitive = []
        for item in inputs:
            target = direct if item.get("kind") == "direct" else transitive
            for key in item.get("keys", ()):
                if key not in target:
                    target.append(key)
        result = {
            "kind": "derived",
            "keys": keys,
            "direct_keys": direct,
            "transitive_keys": [key for key in transitive if key not in direct],
            "inputs": inputs,
            "callable": _callable_metadata(binding.function),
            "source": _binding_source_metadata(binding),
            "expression_source_contract": (
                None if binding.expression_source_contract is None
                else binding.expression_source_contract.as_dict()),
        }
        result.update(_binding_resolution(binding, store))
        return result
    result = binding.as_dict()
    result["keys"] = keys
    result.update(_binding_resolution(binding, store))
    return result


def page_state_keys(root, explicit=()):
    # The explicit keys, then those the bindings in the declarations' public
    # fields read (declared fields in the engine record, the rest in __dict__).
    return _unique_keys(tuple(_unique_keys(explicit)) + _RESOLVER.declared_keys(root.walk_declarations()))
