## Complete option-button surfaces with a native indexed selection icon.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from ui.bindings import resolve
from ui.components import Button

from . import icons


class OptionButton(Button):
    """Keep caption and checkmark together during native press feedback."""

    TEXT_INSET = 52
    ICON_SIZE = 24

    def _surface(self, renderer, bounds, label, state, font, selected):
        background, border, text = renderer.BUTTON_COLORS[state]
        commands = [
            renderer.fill(*bounds, background),
            renderer.stroke(*bounds, border, 2),
            renderer.text(bounds.center_x, bounds.center_y, label, text, font, "center", "middle",
                          max_width=max(1, bounds.width - 2 * self.TEXT_INSET),
                          max_height=max(1, bounds.height - 12), wrap=True, truncate=True),
        ]
        if selected:
            commands.append(icons.render(renderer, "controls", "check", bounds.x + 22,
                                         bounds.center_y - self.ICON_SIZE // 2, (text,)))
        return commands

    def draw(self, renderer, state, bounds):
        label = resolve(self.label, state)
        button_state = resolve(self.state, state)
        font = resolve(self.font, state) if self.font is not None else "JetBrainsMono 8pt"
        selected = button_state == "selected"
        normal = self._surface(renderer, bounds, label, button_state, font, selected)
        if button_state in ("disabled", "busy"):
            # The standard path also removes a previously active hitbox and feedback surface.
            renderer.button(resolve(self.action, state), *bounds, "", state=button_state, font=font)
            return normal
        pressed = self._surface(renderer, bounds, label, "pressed", font, selected)
        return renderer.button_surface(resolve(self.action, state), *bounds, normal, pressed)
