## Shared screen composition and layer lifecycle.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from .bindings import StateStore, page_state_keys
from .engine import NATIVE_METHODS, BinaryCommand, CommandBatch, ScreenFrames
from .identity import serialize_key
from .layout import DeclarativePage, Dirty, Node, Overlay, PageTree, Rect


class ScreenLayer:
    """One independently owned visual instance, retained while suspended."""

    def __init__(self, node, retain_background=False):
        if not isinstance(node, Node):
            raise TypeError("screen layer content must be a Node")
        self.node = node
        # Opt in only for painters that restore their own foreground pixels.
        self.retain_background = bool(retain_background)
        self.suspended = False


class ImperativeSurface(Node):
    """A node an imperative painter draws: its render runs the painter and
    ignores the layout. A screen root whose content and layers are all
    imperative surfaces draws them through its ScreenFrames, recording each
    painter's output when the frame places it."""

    paints_pixels = True
    covers_bounds = True


class _Recorded(Node):
    """An imperative surface's place in the screen's frames: the output its
    painter last recorded, which the engine draws there."""

    paints_pixels = True
    covers_bounds = True
    record_fields = ("commands",)
    draw = NATIVE_METHODS["recorded"]["draw"]

    def __init__(self):
        super().__init__()
        self.commands = None


class _PageSlot(ImperativeSurface):
    """The place of a declarative page in the screen tree.

    The page draws with its own tree, which keeps its layout, styles and
    action table between shows. Building one screen tree around the page's
    root instead walks the whole page at every show and layer change (4-6
    times the cost of drawing the kept page, measured on the host).
    """

    def __init__(self, page, title, back):
        super().__init__()
        self.page = page
        self.title = title
        self.back = back

    def render(self, renderer, state, layout):
        commands = [] if self.title is None else list(renderer.begin_page(self.title, back=self.back))
        commands += self.page.draw(renderer, self.page.state)
        return commands


class ScreenRoot:
    """Own output admission and compose the page and transient layers.

    Declarative updates use the existing page tree's damage and modal rules.
    A modal layer defers background-only paints. Retained foreground painters
    can open and update over existing pixels; resized panels, page replacement
    and output recovery restore the composition. Closing a layer uses current
    data without keeping a second queue of background invalidations.
    An imperative page may submit a delta through the renderer. Such a delta
    is accepted only when the content explicitly allows deltas and there are
    no active layers; otherwise it requests a tree repaint subject to the
    same modal admission rule.
    This adapter lets products migrate their page bodies independently.

    The content is a node or a declarative page. A page keeps its values in
    its own state: the product writes them there and paints, and a delta
    draws the page's changes while no layer is visible. Under a modal layer
    the changes wait for the layer to close; under other layers the
    composition repaints. update() writes the layers' values.

    select_content, when given, returns (content, title, back) for the
    product's current state; each paint shows what it returns.

    While the content and every visible layer are imperative surfaces, the
    root draws through its ScreenFrames (_paint_screen): the engine composes
    them by its layer rule and says what the next frame draws, and the
    surfaces it places record their painters' output. Anything else draws
    through _paint_frame.
    TODO(1P-5c): remove _paint_frame and its PageTree once the remaining
    content and layers are native (the page with 1P-5d, Dialog with the
    stage 1 dialog piece); only Designer scenes still reach it.
    """

    def __init__(self, renderer, content, bounds, page_id, title="", chrome=True,
                 select_content=None):
        self.renderer = renderer
        self.content = None
        self._content_node = None
        self._content_chrome = None
        self._page = None
        self.bounds = bounds if isinstance(bounds, Rect) else Rect(*bounds)
        self.page_id = page_id
        self.title = title
        self.chrome = bool(chrome)
        self.layers = []
        self.visible_layers = ()
        self._visible_content = None
        self._repaint_pending = False
        self._draining_repaint = False
        self.painting = False
        self._rebuild = True
        self._tree = None
        self._values = {}
        self._select_content = select_content
        # The screen path: its frames, their tree, each surface's recorded
        # node (content by (node, title, back), layers by layer), the nodes
        # the tree holds, the layers' state, and whether its next frame
        # must draw whole (the panel shows something it did not draw).
        self._screen = None
        self._screen_tree = None
        self._recorded = {}
        self._shown = ()
        self._screen_state = None
        self._screen_schema = ()
        self._screen_redraw = True
        self._screen_path = None
        self.replace_content(content)
        renderer.configure_frame_owner(self)

    def open(self, layer, replace=None, paint=True, *, before=None):
        if not isinstance(layer, ScreenLayer):
            raise TypeError("screen layer must be a ScreenLayer instance")
        if replace is not None:
            self.close(replace, paint=False)
        if layer not in self.layers:
            if before is None:
                self.layers.append(layer)
            else:
                self.layers.insert(self.layers.index(before), layer)
        self._rebuild = True
        return self.paint() if paint else True

    def close(self, layer, paint=True):
        if layer not in self.layers:
            return False
        self.layers.remove(layer)
        self._rebuild = True
        if paint:
            self.paint()
        return True

    def suspend(self, layer, suspended=True, paint=True):
        if layer not in self.layers or layer.suspended == bool(suspended):
            return False
        layer.suspended = bool(suspended)
        self._rebuild = True
        if paint:
            self.paint()
        return True

    def replace_content(self, content, title=None, back=False):
        """Show content: a node, or a declarative page with its title and back.

        Showing the same content with the same title and back keeps the surface.
        """
        if content is self.content and self._content_chrome == (title, bool(back)):
            return
        if isinstance(content, DeclarativePage):
            if title is not None and self.chrome:
                raise ValueError("a page title needs a screen root without chrome")
            self._content_node = _PageSlot(content, title, bool(back))
            self._page = content
        elif isinstance(content, Node):
            if title is not None:
                raise ValueError("only page content has a title")
            self._content_node = content
            self._page = None
        else:
            raise TypeError("screen content must be a Node or a DeclarativePage")
        self.content = content
        self._content_chrome = (title, bool(back))
        self._rebuild = True

    def update(self, values=None):
        if values is not None:
            self._values.update(values)
        return self.paint()

    def invalidate(self, node=None):
        (self._content_node if node is None else node).invalidate(Dirty.PAINT)

    def surface_cleared(self):
        """The renderer cleared the panel (its start): nothing composed before
        is visible, so the next paint draws the whole composition."""
        self._rebuild = True
        self._screen_redraw = True
        self.visible_layers = ()
        self._visible_content = None

    def paint(self, receipt=None, force=False, receipt_phase="presented"):
        if self.painting or not self.renderer.render_allowed:
            self._repaint_pending = self._repaint_pending or force
            return False
        force = force or self._repaint_pending
        self._repaint_pending = False
        self.painting = True
        try:
            if self._select_content is not None:
                content, title, back = self._select_content()
                self.replace_content(content, title=title, back=back)
            screen = self._surfaces_only()
            if screen != self._screen_path:
                # The other path drew last: neither keeps what is on the panel.
                self._screen_path = screen
                self._rebuild = self._screen_redraw = True
            if screen:
                result = self._paint_screen(receipt, force, receipt_phase)
            else:
                result = self._paint_frame(receipt, force, receipt_phase)
        finally:
            self.painting = False
        return self._drain_repaint() or result

    def _drain_repaint(self):
        if (not self._repaint_pending or self._draining_repaint
                or not self.renderer.render_allowed):
            return False
        # One recovery follows the current send. If even that surface cannot
        # enter the queue, retain the request for the next caller instead of
        # recursively retrying while critical output still occupies it.
        self._draining_repaint = True
        try:
            return self.paint(force=True)
        finally:
            self._draining_repaint = False

    def _paint_frame(self, receipt, force, receipt_phase="presented"):
        layers = tuple(layer for layer in self.layers if not layer.suspended)
        same_surface = self.content is self._visible_content and layers == self.visible_layers
        restore = force or self.renderer.header_action_changed
        mode = "full" if self._rebuild or restore else "delta"
        foreground = None
        try:
            if mode == "full":
                previous_schema = () if self._tree is None else self._tree.state_schema
                self._tree = PageTree(
                    Overlay(self._content_node, *(layer.node for layer in layers)),
                    self.bounds, page_id=self.page_id,
                    state_schema=tuple(key for layer in self.layers
                                       for key in page_state_keys(layer.node)))
                # Suspended instances retain their values; closed instances release them.
                for key in previous_schema:
                    if key not in self._tree.state_schema:
                        self._values.pop(key, None)
            self._tree.state.update(self._values)
            for layer in layers:
                layer.node.update(self._tree.state)
            modal_index = next((index for index in reversed(range(len(layers)))
                                if layers[index].node.input_blocked), None)
            if mode == "delta" and modal_index is not None:
                if all(layer.node.is_clean for layer in layers[modal_index:]):
                    self.renderer.discard_surface()
                    return False
                if all(layer.retain_background for layer in layers[modal_index:]):
                    mode = "update_foreground"
                    foreground = tuple(layer.node for layer in layers[modal_index:])
            elif mode == "delta" and layers and self._page_changed():
                # Layers are drawn again over the page's changed pixels.
                return self._paint_frame(receipt, True, receipt_phase)
            elif (mode == "full" and not restore
                  and self.content is self._visible_content and not self.visible_layers
                  and len(layers) == 1 and modal_index == 0 and layers[0].retain_background):
                mode = "open_foreground"
                foreground = (layers[0].node,)

            if (mode == "delta" and not layers and self._page_changed()
                    and self._content_node.is_clean):
                # The page's own update frame replays its input itself.
                commands = self._page.update(self.renderer)
                accepted = bool(commands) and self.renderer.send(
                    commands, receipt=receipt, receipt_phase=receipt_phase)
                self._rebuild = bool(commands) and not accepted
                return accepted or not commands

            with self.renderer.compose(preserve_input=same_surface,
                                       retain_background=mode == "update_foreground") as frame:
                commands = []
                if mode in ("full", "open_foreground"):
                    if self.chrome and mode == "full":
                        commands += self.renderer.begin_page(self.title)
                    else:
                        self.renderer.invalidate_input_generation()
                if mode == "delta":
                    commands += self._tree.update(self.renderer, self._values)
                else:
                    commands += self._tree.draw(
                        self.renderer, self._values,
                        reuse_layout=mode == "update_foreground",
                        reuse_styles=mode == "update_foreground", render_from=foreground)
                if commands:
                    self.renderer.send(
                        commands, receipt=receipt, receipt_phase=receipt_phase,
                        kind="surface" if mode == "full" else (
                            None if mode == "delta" else "state"),
                        key="foreground" if mode == "update_foreground" else None)
            if frame["background_changed"]:
                # Unknown or changed panel bounds require restoring exposed pixels.
                return self._paint_frame(receipt, True, receipt_phase)
            accepted = frame["accepted"]
            if accepted:
                self.visible_layers = layers
                self._visible_content = self.content
            self._rebuild = bool(commands) and not accepted
            return accepted or not commands
        except BaseException:
            self.renderer.discard_surface()
            self._rebuild = True
            raise

    def _surfaces_only(self):
        return (isinstance(self._content_node, ImperativeSurface)
                and all(isinstance(layer.node, ImperativeSurface)
                        for layer in self.layers if not layer.suspended))

    def _screen_layers(self, layers):
        """Set the screen tree's recorded nodes for the content and layers.
        Returns them with their sources and whether they changed."""
        keys = [(self._content_node,) + self._content_chrome] + list(layers)
        nodes = tuple(self._recorded.get(key) or _Recorded() for key in keys)
        changed = nodes != self._shown
        if changed:
            self._recorded = dict(zip(keys, nodes))
            self._screen_tree.children = nodes
            self._screen_tree._adopt(*nodes)
            self._screen_tree.invalidate_layout()
            self._shown = nodes
            schema = tuple(key for layer in self.layers for key in page_state_keys(layer.node))
            # Suspended instances retain their values; closed instances release them.
            for key in self._screen_schema:
                if key not in schema:
                    self._values.pop(key, None)
            self._screen_schema = schema
            self._screen_state = StateStore(schema)
        sources = [(self._content_node, nodes[0], None)]
        sources += [(layer.node, node, layer) for layer, node in zip(layers, nodes[1:])]
        return sources, changed

    def _paint_screen(self, receipt, force, receipt_phase="presented"):
        """Draw the surfaces through the screen's frames: the modes of
        _paint_frame, decided by the engine's layer rule (planned())."""
        layers = tuple(layer for layer in self.layers if not layer.suspended)
        same_surface = self.content is self._visible_content and layers == self.visible_layers
        if (not layers and same_surface and not (
                force or self.renderer.header_action_changed or self._rebuild
                or self._screen_redraw)
                and self._content_node.is_clean and self._page_changed()):
            # The page's own update frame replays its input itself. The screen's
            # tree stays clean: its next frame that draws the content records it
            # again.
            commands = self._page.update(self.renderer)
            accepted = bool(commands) and self.renderer.send(
                commands, receipt=receipt, receipt_phase=receipt_phase)
            self._rebuild = self._screen_redraw = bool(commands) and not accepted
            return accepted or not commands
        if self._screen is None:
            self._screen_tree = Overlay()
            self._screen = ScreenFrames(self._screen_tree, None, self.bounds)
        screen = self._screen
        screen.begin()
        try:
            sources, changed = self._screen_layers(layers)
            if (force or self.renderer.header_action_changed or self._screen_redraw
                    or (self._rebuild and not changed)):
                screen.redraw()
            self._screen_redraw = False
            state = self._screen_state
            state.update(self._values)
            for source, node, layer in sources:
                source.update(state)
                node._blocks_input = source.input_blocked
                node._retains_background = layer is not None and layer.retain_background
                if not source.is_clean:
                    node.invalidate()
                    source.clear_dirty()
            content = sources[0][1]
            if content.is_clean and self._page_changed():
                # The page's values changed under layers: draw it again.
                content.invalidate()
            planned = screen.planned()
        except BaseException:
            self._abandon_screen()
            raise
        if planned is None:
            screen.end(self.renderer)
            self.renderer.discard_surface()
            return not any(node._blocks_input for _source, node, _layer in sources[1:])
        mode, foreground = planned
        try:
            opening = mode == "foreground" and changed
            retained = mode == "foreground" and not changed
            with self.renderer.compose(preserve_input=same_surface,
                                       retain_background=retained) as frame:
                commands = []
                if mode == "whole" or opening:
                    if self.chrome and mode == "whole":
                        commands += self.renderer.begin_page(self.title)
                    else:
                        self.renderer.invalidate_input_generation()
                if mode != "repaint":
                    # As the composition's draws named their page.
                    self.renderer.set_semantic_page(serialize_key(self.page_id))
                for source, node, _layer in sources:
                    if (mode == "whole" or (mode == "foreground" and node in foreground)
                            or (mode == "repaint" and not node.is_clean)):
                        node.commands = CommandBatch(
                            source.render(self.renderer, state, None), BinaryCommand)
                commands += screen.end(self.renderer) or ()
                if commands:
                    self.renderer.send(
                        commands, receipt=receipt, receipt_phase=receipt_phase,
                        kind="surface" if mode == "whole" else (
                            None if mode == "repaint" else "state"),
                        key="foreground" if retained else None)
        except BaseException:
            self._abandon_screen()
            raise
        if frame["background_changed"]:
            # Unknown or changed panel bounds require restoring exposed pixels.
            self._screen_redraw = True
            return self._paint_screen(receipt, True, receipt_phase)
        accepted = frame["accepted"]
        if accepted:
            self.visible_layers = layers
            self._visible_content = self.content
        self._rebuild = bool(commands) and not accepted
        self._screen_redraw = self._rebuild
        return accepted or not commands

    def _abandon_screen(self):
        """A failed screen frame: whatever it drew is unknown, so the next
        frame draws whole."""
        self.renderer.discard_surface()
        self._rebuild = self._screen_redraw = True
        if self._screen is not None and self._screen.open:
            try:
                self._screen.end(self.renderer)
            except RuntimeError:
                pass

    def _page_changed(self):
        return self._page is not None and bool(self._page.state.changed_keys)

    def submit(self, commands, **metadata):
        """Admit a legacy page delta or repaint its surrounding composition."""
        if (self._rebuild or self.visible_layers
                or any(not layer.suspended for layer in self.layers)
                or not getattr(self.content, "accepts_deltas", False)):
            self.renderer.discard_surface()
            self.invalidate()
            return self.paint(receipt=metadata.get("receipt"),
                              receipt_phase=metadata.get("receipt_phase", "presented"))
        self.painting = True
        try:
            result = self.renderer.send(commands, **metadata)
        finally:
            self.painting = False
        return self._drain_repaint() or result
