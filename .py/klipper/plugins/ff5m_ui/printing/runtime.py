## Runtime facade for the declarative active-print page.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import importlib
import sys

from ui.lazy import resolve_lazy_export

from .actions import PrintingAction, PrintingRoute
from .state import PrintingState


_LAZY_EXPORTS = {
    "PREVIEW_IMAGE_PADDING": "page",
    "PrintingRef": "page",
}


def _page_module():
    name = "%s.page" % __package__
    module = sys.modules.get(name)
    if module is not None and not getattr(getattr(module, "__spec__", None), "_initializing", False):
        return module
    return importlib.import_module(name)


def get_page():
    return _page_module().PAGE


_GEOMETRY = []


def rect(ref):
    """Where ref lies on the printing page. The shown page has no layout of its own
    while the frame loop arranges it, and these places do not move with its values,
    so a page laid out once answers."""
    if not _GEOMETRY:
        page = _page_module().create_page()
        page.prepare_layout()
        _GEOMETRY.append(page)
    return _GEOMETRY[0].rect(ref)


def __getattr__(name):
    return resolve_lazy_export(globals(), name, _LAZY_EXPORTS, __package__)


__all__ = (
    "PREVIEW_IMAGE_PADDING", "PrintingAction", "PrintingRef",
    "PrintingRoute", "PrintingState", "get_page", "rect",
)
