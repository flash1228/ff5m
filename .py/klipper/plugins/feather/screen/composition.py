## Product adapters for the shared screen composition tree.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from ui import ImperativeSurface, ScreenLayer


class PaintSurface(ImperativeSurface):
    """Bridge existing page painters while the root owns their output."""

    def __init__(self, painter, accepts_deltas=False):
        super().__init__()
        self.painter = painter
        self.accepts_deltas = accepts_deltas

    def render(self, renderer, state, layout):
        with renderer.collect() as commands:
            self.painter()
        return commands


class DialogInstance(ScreenLayer):
    """Own the accepted dialog content until explicitly closed. The layer is
    tree, a declarative page the screen draws, whose table holds its actions."""

    def __init__(self, kind, content, priority, tree, backdrop=False):
        self.kind = kind
        self.content = content
        self.priority = priority
        super().__init__(tree, retain_background=True, backdrop=backdrop)

    def resolve_action(self, wire_id):
        return self.node.resolve_action(wire_id)
