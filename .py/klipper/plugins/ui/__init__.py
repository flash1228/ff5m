## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

## Public Feather UI package.

__version__ = "2.12.0"
FRAMEWORK_API_VERSION = 2
REFLECTION_SCHEMA_VERSION = "2.4.0"
FRAMEWORK_CAPABILITIES = (
    "typed-identities",
    "stable-key-namespaces",
    "semantic-actions",
    "typed-state",
    "on-demand-reflection",
    "on-demand-source-provenance",
    "binding-source-authoring",
    "source-edit-contracts",
    "call-site-overrides",
    "structural-editing",
    "package-relative-themes",
    "typed-theme-tokens",
    "data-driven-list-templates",
    "list-item-reuse",
    "painted-content-frame",
    "typed-styles",
    "style-inheritance",
    "component-templates",
    "grid-cell-source",
    "modal-popup",
    "fitted-text",
    "slider-knob",
    "labelled-slider",
    "stacked-metrics",
    "page-chrome-geometry",
    "page-bounds-source",
    "single-template-list",
    "page-declarations-source",
    "page-action-references",
    "state-case-properties",
)


def framework_manifest():
    """Return the stable, serializable framework compatibility contract."""
    return {
        "name": "feather-ui",
        "version": __version__,
        "api_version": FRAMEWORK_API_VERSION,
        "reflection_schema_version": REFLECTION_SCHEMA_VERSION,
        "capabilities": list(FRAMEWORK_CAPABILITIES),
    }

_EXPORT_GROUPS = (
    ("screen", ("ImperativeSurface", "ScreenLayer", "ScreenRoot")),
    ("layout_helpers", (
        "centered_button_row", "dialog_pager_bounds",
        "dialog_vertical_bounds", "layout_dialog_text",
    )),
    ("authoring", ("REUSABLE_SOURCE_CONTRACT", "ReusableSourceContract")),
    ("theme", (
        "ThemeColor", "ThemeRole", "ResolvedTheme",
        "DEFAULT_THEME_ROLES", "normalize_theme_token", "resolve_theme",
    )),
    ("theme_catalog", (
        "DEFAULT_THEME", "FALLBACK_THEME", "MAX_THEME_FILE_BYTES",
        "THEME_DIRECTORY", "THEME_SCHEMA_PATH", "USER_THEME_DIRECTORY",
        "ThemeCatalog", "ThemeFileIssue", "ThemeFileTooLarge",
        "ThemeSchemaError", "normalize_theme_name", "validate_theme_data",
    )),
    ("renderer", (
        "CONTENT_BOTTOM", "FOOTER_HEIGHT", "FOOTER_Y",
        "HEADER_BOTTOM", "MAX_ATOMIC_DRAW", "MAX_BATCH_BYTES", "MAX_PENDING_DRAW",
        "SCREEN_HEIGHT", "SCREEN_WIDTH", "PAGE_CHROME_CONTRACT",
        "PageChromeContract", "FeatherRenderer", "RenderSubmission", "rectangles_overlap",
        "ButtonEvent", "TapEvent", "TouchDeviceEvent", "TouchEvent",
    )),
    ("render_receipts", (
        "PendingReceipt", "ReceiptMeasurement", "ReceiptTracker",
        "RenderReceipt", "validate_render_receipt_token",
    )),
    ("identity", (
        "CommandKey", "FrameworkKey", "PageKey", "StateKey",
        "serialize_key",
    )),
    ("actions", (
        "Action", "Back", "CancellationHint", "Command", "CompletionHint",
        "ContinuousMovementHint", "CoolingHint", "DismissToast",
        "DispatchResult",
        "EditText", "HeatingHint", "HomingHint", "Increment", "ItemCommand",
        "MotorStateHint",
        "MovementHint", "Navigate", "ProgressHint", "ProbingHint",
        "Replace", "Router", "SetValue", "SimulationHint", "Toggle",
        "action_metadata", "action_wire_id", "collect_actions",
    )),
    ("bindings", (
        "BINDING_EXPRESSION_CONTRACT", "STATE_DECLARATION_CONTRACT",
        "Binding", "BindingExpressionContract", "DerivedBinding", "DirectBinding",
        "ItemBinding", "ItemScope", "StateDeclarationContract", "StateSpec",
        "StateStore", "bind", "derived", "item", "state", "state_spec",
    )),
    ("numeric_input", ("NumericInputSpec",)),
    ("properties", (
        "CreationFieldSpec", "EditorSpec", "Invalidation", "PropertySpec",
        "RewritePolicy", "SourceSpec", "ValidationSpec",
        "inheritable_style_properties", "property_names", "property_schema",
        "register_inheritable_style_property",
    )),
    ("styles", ("Style", "StyleSheet")),
    ("layout", (
        "EMPTY", "FLEX", "Cell", "Column", "ComponentTemplate", "CreationContract",
        "CreationIdentityContract", "CreationSourceContract",
        "DeclarativePage",
        "Dirty", "Equal", "EqualTracks", "Flex", "Grid", "Insets",
        "LAYOUT_SCHEMA", "LayoutModifierSpec", "LayoutResult", "LayoutSourceContract",
        "List", "ListView", "Node", "Overlay", "Param", "ParamRef", "Template",
        "ACTION_REFERENCE_SOURCE_CONTRACT", "ActionReferenceSourceContract",
        "Override", "PAGE_BOUNDS_SOURCE_CONTRACT", "PAGE_DECLARATIONS_SOURCE_CONTRACT",
        "PAGE_DISCOVERY_CONTRACT", "PageBoundsSourceContract",
        "PageDeclarationsSourceContract", "PageDiscoveryContract",
        "PageTree", "Rect", "Row", "Spacer", "Span",
        "StateCase", "StructureContract", "StructureSourceContract", "Tree", "When", "WrapPanel", "split",
        "param", "subdivision_positions",
    )),
    ("components", (
        "ArrowButton", "Button", "ButtonStyle", "CornerMarks", "Crosshair", "Dialog",
        "DotGrid", "Fill", "Frame", "Hitbox", "JoystickKnob", "Slider", "SliderKnob", "Metric", "Panel", "Popup",
        "NumericKeypad", "ScrollIndicator", "Section", "Stroke", "Text", "ToggleSwitch", "VerticalGauge",
        "VerticalScale",
    )),
)
_EXPORTS = dict(
    (name, module_name)
    for module_name, names in _EXPORT_GROUPS
    for name in names)


def __getattr__(name):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    module = __import__("%s.%s" % (__name__, module_name), fromlist=(name,))
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()).union(_EXPORTS))

__all__ = (
    "REUSABLE_SOURCE_CONTRACT", "ReusableSourceContract",
    "__version__", "FRAMEWORK_API_VERSION", "REFLECTION_SCHEMA_VERSION",
    "FRAMEWORK_CAPABILITIES", "framework_manifest",
    "ThemeColor", "ThemeRole", "ResolvedTheme",
    "DEFAULT_THEME_ROLES", "normalize_theme_token", "resolve_theme",
    "DEFAULT_THEME", "FALLBACK_THEME", "MAX_THEME_FILE_BYTES",
    "THEME_DIRECTORY", "THEME_SCHEMA_PATH", "USER_THEME_DIRECTORY",
    "ThemeCatalog", "ThemeFileIssue", "ThemeFileTooLarge",
    "ThemeSchemaError", "normalize_theme_name", "validate_theme_data",
    "CONTENT_BOTTOM", "FOOTER_HEIGHT", "FOOTER_Y",
    "HEADER_BOTTOM", "MAX_ATOMIC_DRAW", "MAX_PENDING_DRAW",
    "SCREEN_HEIGHT", "SCREEN_WIDTH", "PAGE_CHROME_CONTRACT", "PageChromeContract",
    "MAX_BATCH_BYTES", "RenderSubmission",
    "ButtonEvent", "TapEvent", "TouchDeviceEvent", "TouchEvent",
    "PendingReceipt", "ReceiptMeasurement", "ReceiptTracker",
    "RenderReceipt", "validate_render_receipt_token",
    "rectangles_overlap", "FeatherRenderer",
    "FrameworkKey", "PageKey", "StateKey", "CommandKey", "serialize_key",
    "Action", "Navigate", "Back", "DismissToast", "Replace", "SetValue",
    "Toggle", "EditText", "ItemCommand",
    "Increment", "Command", "SimulationHint", "HomingHint",
    "MovementHint", "ContinuousMovementHint", "MotorStateHint", "HeatingHint",
    "CoolingHint", "ProbingHint", "ProgressHint", "CompletionHint",
    "CancellationHint", "DispatchResult", "Router", "action_metadata",
    "action_wire_id", "collect_actions",
    "BindingExpressionContract", "StateDeclarationContract",
    "BINDING_EXPRESSION_CONTRACT", "STATE_DECLARATION_CONTRACT",
    "Binding", "DirectBinding", "DerivedBinding", "ItemBinding", "ItemScope",
    "StateSpec", "StateStore", "state", "state_spec", "bind", "derived", "item",
    "NumericInputSpec",
    "Invalidation", "RewritePolicy", "EditorSpec", "SourceSpec",
    "ValidationSpec", "CreationFieldSpec", "PropertySpec",
    "inheritable_style_properties", "property_names", "property_schema",
    "register_inheritable_style_property", "Style", "StyleSheet",
    "EMPTY", "FLEX", "Cell", "Column", "ComponentTemplate", "CreationContract",
    "CreationIdentityContract", "CreationSourceContract", "DeclarativePage",
    "Dirty", "Equal", "EqualTracks", "Flex", "Grid", "Insets",
    "LAYOUT_SCHEMA", "LayoutModifierSpec", "LayoutResult",
    "LayoutSourceContract", "List", "ListView",
    "Node", "Overlay", "Override", "Param", "ParamRef", "param", "PAGE_DISCOVERY_CONTRACT", "Template",
    "PAGE_BOUNDS_SOURCE_CONTRACT", "PageBoundsSourceContract",
    "PAGE_DECLARATIONS_SOURCE_CONTRACT", "PageDeclarationsSourceContract",
    "ACTION_REFERENCE_SOURCE_CONTRACT", "ActionReferenceSourceContract",
    "PageDiscoveryContract", "PageTree", "Rect", "Row",
    "Spacer", "Span", "StateCase", "StructureContract", "StructureSourceContract", "Tree", "When", "WrapPanel", "split",
    "subdivision_positions", "ArrowButton", "Button",
    "ButtonStyle", "CornerMarks", "Crosshair", "Dialog", "DotGrid",
    "Fill", "Frame", "Hitbox", "JoystickKnob", "Slider", "SliderKnob", "Metric", "NumericKeypad", "Panel", "Popup",
    "ScrollIndicator", "Section", "Stroke", "Text", "ToggleSwitch", "VerticalGauge", "VerticalScale",
)
