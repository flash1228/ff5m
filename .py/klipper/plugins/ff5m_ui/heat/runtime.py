## Runtime facade for the declarative Heat/Fan page.

import importlib

from ui.lazy import resolve_lazy_export
from .actions import HeatCommand, LEGACY_ACTIONS
from .state import HeatState


_PAGES = {}
_LAZY_EXPORTS = {"HeatRef": "page"}


def get_page(materials=()):
    key = tuple(materials)
    page = _PAGES.get(key)
    if page is None:
        module = importlib.import_module("%s.page" % __package__)
        page = _PAGES[key] = module.create_page(key)
    return page


def __getattr__(name):
    return resolve_lazy_export(
        globals(), name, _LAZY_EXPORTS, __package__)


__all__ = (
    "HeatCommand", "HeatRef", "HeatState", "LEGACY_ACTIONS",
    "get_page",
)
