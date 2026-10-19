## Mod-parameter editors for Feather.
##
## Copyright (C) 2025-2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from ui.lazy import LazyModule
from ff5m_ui.screen import ScreenPage

from feather.screen.pagination import Pagination


mod_ui = LazyModule("feather.settings.mod")
parameter_ui = LazyModule("ff5m_ui.parameter_controls")


class ModEditorPagesMixin:
    def _render_parameter_options(self):
        param = self.mod_parameter
        if param is None:
            self._show_page(ScreenPage.MOD_SETTINGS)
            return
        if param.key == "feather_theme":
            self._render_theme_options(param)
            return
        commands = self.renderer.begin_page(str(param.label), back=True)
        bounds = parameter_ui.content_bounds(self.renderer)
        pagination = Pagination(
            self.parameter_option_entries, getattr(self, "parameter_options_page_index", 0),
            parameter_ui.options_capacity(param, bounds))
        self.parameter_options_page_index = pagination.page
        commands += parameter_ui.create_options_page(
            param, pagination, self.selected_parameter_option, bounds=bounds).draw(self.renderer)
        self.renderer.send(commands)

    def _render_theme_options(self, param):
        if self.selected_parameter_option not in self.parameter_options:
            self.selected_parameter_option = "DEFAULT"
        bounds = parameter_ui.content_bounds(self.renderer)
        pagination = Pagination(
            self.parameter_option_entries, getattr(self, "parameter_options_page_index", 0),
            parameter_ui.theme_capacity(bounds))
        self.parameter_options_page_index = pagination.page
        commands = self.renderer.begin_page(str(param.label), back=True)
        commands += parameter_ui.create_theme_page(
            param, pagination, self.selected_parameter_option, bounds=bounds).draw(self.renderer)
        self.renderer.send(commands)

    def _render_mod_value(self):
        param = self.mod_parameter
        if param is None:
            self._show_page(ScreenPage.MOD_SETTINGS)
            return
        kind = mod_ui.parameter_kind(param)
        commands = self.renderer.begin_page("Edit value", back=True)
        if kind in ("int", "float"):
            commands += self._render_mod_numeric_keys(param)
            self.renderer.send(commands)
            return
        commands += parameter_ui.create_text_page(
            param, self.mod_edit_value, self.mod_edit_cursor, self.mod_keyboard_symbols, self.mod_keyboard_shift,
            bounds=parameter_ui.content_bounds(self.renderer)).draw(self.renderer)
        self.renderer.send(commands)

    def _render_mod_numeric_keys(self, param):
        return parameter_ui.create_numeric_page(
            param, self.mod_edit_value, bounds=parameter_ui.content_bounds(self.renderer, kind="numeric")).draw(self.renderer)
