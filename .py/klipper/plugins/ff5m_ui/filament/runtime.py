"""Lazy runtime facade for declarative filament pages."""

import importlib

from .actions import FilamentCommand, LEGACY_ACTIONS
from .state import FilamentState


_MATERIAL_PAGES = {}
_ACTION_PAGES = {}


def get_material_page(profiles=()):
    key = tuple(profiles)
    page = _MATERIAL_PAGES.get(key)
    if page is None:
        module = importlib.import_module("%s.material.page" % __package__)
        page = _MATERIAL_PAGES[key] = module.create_page(key)
    return page


def get_action_page(from_pause=False):
    key = bool(from_pause)
    page = _ACTION_PAGES.get(key)
    if page is None:
        module = importlib.import_module("%s.action.page" % __package__)
        page = _ACTION_PAGES[key] = module.create_page(key)
    return page


__all__ = (
    "FilamentCommand", "FilamentState", "LEGACY_ACTIONS",
    "get_material_page", "get_action_page",
)

