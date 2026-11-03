## Shared screen composition and layer lifecycle.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from .bindings import StateStore, page_state_keys
from .engine import NATIVE_METHODS, BinaryCommand, CommandBatch, ScreenFrames
from .footer import FOOTER_BOUNDS
from .header import Header
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
    ignores the layout. The screen root records the painter's output when
    the frame places it."""

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


class _PageSlot(Overlay):
    """The place of a declarative page in the screen tree: its header (with a
    title) under the page's root, placed at the page's bounds.

    The page keeps its state, actions, styles and update pass; its root owns
    the page's tree flags while the screen holds it, and the screen draws the
    page's nodes with the page's state. The header (header.Header) is a tree
    of its own with the page's title, back button and header button (the
    page's header_button, if any).
    """

    def __init__(self, page, title, back):
        bounds = page.bounds
        self.header = None if title is None else Header(title, back, getattr(page, "header_button", None))
        self._frame = Overlay(page.root).offset(bounds.x, bounds.y).size(bounds.width, bounds.height)
        super().__init__(*self._nodes())
        self.page = page
        self.title = title
        self.back = back

    def _nodes(self):
        return (self.header.node, self._frame) if self.header is not None else (self._frame,)

    def apply_header(self, renderer):
        """Inside the screen's transaction: the header's values, and its new tree for a new header action."""
        if self.header is None or not self.header.apply(renderer):
            return
        for node in self.children:
            if node is not self._frame:
                node.parent = None
        self.children = self._nodes()
        self._adopt(*self.children)
        self.invalidate_layout()

    def drew(self, layout):
        """The screen drew the slot and published its layout."""
        self.page.screen_drew(layout)
        if self.header is not None:
            self.header.page.screen_drew(layout)


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
    composition repaints. update() writes the values the content node and
    the layers read.

    select_content, when given, returns (content, title, back) for the
    product's current state; each paint shows what it returns.
    write_content, when given, writes the content's values; each paint runs
    it inside its transaction, before the page's pass. A frame loop may take
    the screen's tree between transactions on its own thread, so the tree and
    its state change only there: invalidate() waits for the next paint too.

    The root draws through its ScreenFrames (_paint_screen): the engine
    composes the content and the layers by its layer rule and says what the
    next frame draws. A page's nodes are the screen tree's; every other node
    is recorded: the frame places the output its source last drew, which an
    imperative surface's painter draws and any other node's own tree draws
    (_record), on the reactor until such nodes are native. The renderer's
    footer is a persisting layer of that tree, between the content and the
    layers, while it shows and its strip lies in the screen; the content is
    then sized above it.
    """

    def __init__(self, renderer, content, bounds, page_id, select_content=None, write_content=None):
        self.renderer = renderer
        self.content = None
        self._content_node = None
        self._content_chrome = None
        self._page = None
        self.bounds = bounds if isinstance(bounds, Rect) else Rect(*bounds)
        self.page_id = page_id
        self.layers = []
        self.visible_layers = ()
        self._visible_content = None
        # A paint asked for while one ran, which may have read its values already (and whether it was forced).
        self._paint_again = False
        self._force_again = False
        self.painting = False
        self._rebuild = True
        self._values = {}
        self._select_content = select_content
        self._write_content = write_content
        # Nodes invalidate() asked to repaint (None: the content), applied inside the next paint.
        self._invalidated = []
        # The screen's frames, their tree, each recorded node (content by
        # (node, title, back), layers by layer), the nodes the tree holds, the
        # state the recorded sources read, the trees that draw sources other
        # than imperative surfaces, and whether the next frame must draw whole
        # (the panel shows something it did not draw).
        self._screen = None
        self._screen_tree = None
        self._recorded = {}
        self._shown = ()
        self._screen_state = None
        self._screen_schema = ()
        self._trees = {}
        self._screen_redraw = True
        # The renderer's header action as the last accepted frame showed it. Imperative painters (the content's
        # begin_page, a dialog that keeps the header action) draw it only in a whole frame.
        self._header_action = None
        # The renderer's dialog bounds as the last accepted frame drew them: a retained painter keeps the pixels
        # around its dialogs only while they stay.
        self._dialog_bounds = ()
        # The footer's place in the screen tree: its root at the footer's bounds.
        self._footer_slot = None
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
            # Made inside the screen's transaction (_content): it adopts the page's root.
            self._content_node = None
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
        """Repaint node (the content when None) at the next paint, which
        applies it inside its transaction."""
        self._invalidated.append(node)

    def _content(self):
        """The content's node; a page's slot is made on first use, inside
        the screen's transaction, since it adopts the page's root, which the
        screen's tree may still hold."""
        if self._content_node is None:
            self._content_node = _PageSlot(self._page, *self._content_chrome)
        return self._content_node

    def _write_inside(self):
        """Inside the paint (the screen's transaction): the
        content's values and the repaints invalidate() asked for."""
        if self._write_content is not None:
            self._write_content()
        invalidated, self._invalidated = self._invalidated, []
        for node in invalidated:
            node = self._content() if node is None else node
            if isinstance(node, _PageSlot):
                # The slot does not cover its bounds (begin_page leaves the footer's strip alone), and a
                # repaint would fill them: laid out again, it is drawn as a whole frame draws it.
                node.invalidate_layout()
            else:
                node.invalidate(Dirty.PAINT)

    def shown_input(self):
        """The input Typer holds of the screen (ScreenFrames.shown_input), or
        None before its first frame."""
        if self._screen is None:
            return None
        return self._screen.shown_input()

    def surface_cleared(self):
        """The renderer cleared the panel (its start): nothing composed before
        is visible, so the next paint draws the whole composition."""
        self._rebuild = True
        self._screen_redraw = True
        self.visible_layers = ()
        self._visible_content = None

    def paint(self, receipt=None, force=False, receipt_phase="presented"):
        """One transaction of the screen: the content selected and written, the
        layers' values, the invalidations, the painters of what the frame paints,
        then its frame. A paint asked for inside one (a painter, write_content, a
        header change) runs once that one ends. While output is frozen nothing is
        drawn: the thaw clears the panel, and the paint after it draws it all."""
        if self.painting:
            self._paint_again = True
            self._force_again = self._force_again or force
            return False
        if not self.renderer.render_allowed:
            return False
        result = self._paint_once(receipt, force, receipt_phase)
        return self._paint_asked() or result

    def _paint_once(self, receipt, force, receipt_phase):
        self.painting = True
        try:
            if self._select_content is not None:
                content, title, back = self._select_content()
                self.replace_content(content, title=title, back=back)
            return self._paint_screen(receipt, force, receipt_phase)
        finally:
            self.painting = False

    def _paint_asked(self):
        """The paint asked for while one ran, drawn now; once, since a paint that
        asks for itself each time would not end."""
        if not self._paint_again or not self.renderer.render_allowed:
            return False
        force, self._paint_again, self._force_again = self._force_again, False, False
        return self._paint_once(None, force, "presented")

    def _screen_layers(self, layers):
        """Set the screen tree's nodes for the content and layers: a page's
        slot, or a recorded node for each surface, with the footer between
        them while it shows. Returns the content's and layers' nodes with their
        sources and whether they changed."""
        content = self._content()
        keys = [(content,) + self._content_chrome] + list(layers)
        slot = content if isinstance(content, _PageSlot) else None
        nodes = (slot or self._recorded.get(keys[0]) or _Recorded(),) + tuple(
            self._recorded.get(key) or _Recorded() for key in keys[1:])
        changed = nodes != self._shown
        if changed:
            for node in self._shown:
                if node not in nodes:
                    # Off the screen its dirt (a page written while hidden) reaches no frame.
                    node.parent = None
            self._recorded = dict(zip(keys, nodes))
            self._shown = nodes
            sources = ((content,) if slot is None else ()) + tuple(layer.node for layer in self.layers)
            self._trees = {source: tree for source, tree in self._trees.items() if source in sources}
            schema = tuple(dict.fromkeys(key for source in sources for key in page_state_keys(source)))
            # Suspended instances retain their values; closed instances release them.
            for key in self._screen_schema:
                if key not in schema:
                    self._values.pop(key, None)
            self._screen_schema = schema
            self._screen_state = StateStore(schema)
        self._place_footer()
        sources = [(content, nodes[0], None)]
        sources += [(layer.node, node, layer) for layer, node in zip(layers, nodes[1:])]
        return sources, changed

    def _paint_screen(self, receipt, force, receipt_phase="presented"):
        """Draw the content and the layers through the screen's frames, in the
        mode the engine's layer rule decides (planned())."""
        layers = tuple(layer for layer in self.layers if not layer.suspended)
        same_surface = self.content is self._visible_content and layers == self.visible_layers
        if self._screen is None:
            self._screen_tree = Overlay()
            self._screen = ScreenFrames(self._screen_tree, None, self.bounds)
        screen = self._screen
        screen.begin()
        try:
            self._write_inside()
            shown = self._shown[:1]
            sources, changed = self._screen_layers(layers)
            slot = sources[0][1] if isinstance(sources[0][1], _PageSlot) else None
            if slot is not None:
                slot.apply_header(self.renderer)
            header_action = getattr(self.renderer, "_header_action", None)
            # A page's slot alone takes a new header action as its own relayout (apply_header).
            redraw = (force or self._screen_redraw or (self._rebuild and not changed)
                      or (header_action != self._header_action and (slot is None or bool(layers))))
            if self._screen_redraw:
                # The panel's pixels are gone: every layer is drawn, the footer too.
                screen.redraw()
            elif redraw:
                # The composition draws whole; the footer's pixels survive it.
                screen.draw_whole()
            self._screen_redraw = False
            page = None if slot is None else slot.page
            if page is not None:
                # A frame that draws the page anew prepares it as its draw does.
                page.prepare_frame(redraw or shown != self._shown[:1])
            state = self._screen_state
            state.update(self._values)
            for source, node, layer in sources:
                if source is node:
                    continue  # a page's slot: its nodes are the screen's
                source.update(state)
                node._blocks_input = source.input_blocked
                node._retains_background = layer is not None and layer.retain_background
                if not source.is_clean:
                    node.invalidate()
                    source.clear_dirty()
            planned = screen.planned()
        except BaseException:
            self._abandon_screen()
            raise
        if planned is None:
            screen.end(self.renderer)
            self.renderer.discard_surface()
            return not any(node._blocks_input for _source, node, _layer in sources[1:])
        try:
            mode = self._paint_nodes(planned, changed, same_surface, sources, slot, state)
            critical = self.renderer.painted_critical
            commands = list(screen.end(self.renderer) or ())
        except BaseException:
            self._abandon_screen()
            raise
        accepted = bool(commands) and self.renderer.submit_frame(
            commands, receipt=receipt, receipt_phase=receipt_phase, critical=critical)
        if slot is not None and mode in ("whole", "relayout"):
            slot.drew(screen.layout)
        if accepted or not commands:
            # A frame with nothing to draw leaves the panel as the composition shows it.
            self.renderer.keep_surface()
            self.visible_layers = layers
            self._visible_content = self.content
            self._header_action = header_action
            self._dialog_bounds = self.renderer.dialog_bounds
        else:
            self.renderer.discard_surface()
        self._rebuild = bool(commands) and not accepted
        self._screen_redraw = self._rebuild
        return accepted or not commands

    def _paint_nodes(self, planned, changed, same_surface, sources, slot, state):
        """Inside the transaction, what its frame needs from the reactor before
        it ends: for another surface a new input generation and the page's start,
        then the output of the recorded nodes it paints. Returns the frame's mode."""
        renderer, (mode, foreground) = self.renderer, planned
        retained = mode == "foreground" and not changed
        with renderer.painters(retain_background=retained):
            if (mode == "whole" or (mode == "foreground" and changed)) and not same_surface:
                renderer.invalidate_input_generation()
                if mode == "whole" and slot is not None and slot.header is not None:
                    # The page starts on the panel, as its painter's begin_page starts it.
                    renderer.show_page(slot.title, back=slot.back)
            if slot is not None and mode in ("whole", "relayout"):
                renderer.set_semantic_page(slot.page.page_id)
            elif mode != "repaint":
                # As the composition's draws named their page.
                renderer.set_semantic_page(serialize_key(self.page_id))
            for source, node, _layer in sources:
                if source is node:
                    continue  # a page's slot: its nodes are the screen's
                if (mode == "whole" or (mode == "foreground" and node in foreground)
                        or (mode in ("repaint", "relayout") and not node.is_clean)):
                    node.commands = CommandBatch(self._record(source, state), BinaryCommand)
        if mode == "whole":
            # The painters may have shown or hidden the footer (a page begun, a state
            # screen); after the plan only a frame that draws whole takes that.
            self._place_footer()
        bounds = renderer.dialog_bounds
        if retained and (not bounds or bounds != self._dialog_bounds):
            # The retained painters drew over pixels around dialogs of unknown or other bounds: the panel is
            # drawn in full, which restores the exposed background.
            self._screen.redraw()
            return self._paint_nodes(self._screen.planned(), changed, same_surface, sources, slot, state)
        return mode

    def _record(self, source, state):
        """What a recorded node holds: the output of its imperative surface's
        painter (with what it sends meanwhile), or of any other node drawn whole
        by a tree of its own with the screen's values (Python on the reactor,
        until such nodes are native)."""
        if isinstance(source, ImperativeSurface):
            with self.renderer.collect() as sent:
                output = source.render(self.renderer, state, None)
            return list(sent) + list(output)
        tree = self._trees.get(source)
        if tree is None:
            tree = self._trees[source] = PageTree(source, self.bounds, page_id=self.page_id)
        return tree.draw(self.renderer, {key: state[key] for key in tree.state_schema})

    def _place_footer(self):
        """Inside the screen's transaction: the content and layers with the
        footer the screen holds between them (_held_footer), with its latest
        values unless a modal layer covers it, and the content sized above it."""
        slot = self._held_footer()
        if slot is not None and not self._footer_covered():
            self.renderer.footer_chrome.apply()
        nodes = self._shown[:1] + ((slot,) if slot is not None else ()) + self._shown[1:]
        if nodes == tuple(self._screen_tree.children):
            return
        content = self._shown[0]
        if not isinstance(content, _PageSlot):
            # A recorded node covers its bounds. A page's slot is sized by its own nodes: the header draws
            # only above the footer, but its wake region covers the screen, as begin_page's does.
            height = FOOTER_BOUNDS.y - self.bounds.y if slot is not None else None
            if content.layout_options.height != height:
                content.layout_options.height = height
        for node in self._screen_tree.children:
            if node not in nodes:
                node.parent = None
        self._screen_tree.children = nodes
        self._screen_tree._adopt(*nodes)
        self._screen_tree.invalidate_layout()

    def _held_footer(self):
        """The footer's slot the screen holds now, or None: the renderer's footer
        while it shows with values and its strip lies in the screen. Under a modal
        layer, whose scrim covers it, a footer that is not in the screen stays out
        until the layer closes."""
        footer = getattr(self.renderer, "footer_chrome", None)
        node = None if footer is None else footer.node
        bounds, strip = self.bounds, FOOTER_BOUNDS
        holds = (bounds.x <= strip.x and strip.x + strip.width <= bounds.x + bounds.width
                 and bounds.y <= strip.y and strip.y + strip.height <= bounds.y + bounds.height)
        slot = self._footer_slot
        placed = slot is not None and slot in self._screen_tree.children
        if node is None or not holds or (self._footer_covered() and not placed):
            return None
        if slot is None or slot.children[0] is not node:
            slot = self._footer_slot = Overlay(node).offset(
                strip.x - bounds.x, strip.y - bounds.y).size(strip.width, strip.height)
            slot._persists = True
        return slot

    def _footer_joins(self):
        """Whether the footer the screen holds is not in its tree yet."""
        slot = self._held_footer() if self._screen_tree is not None else None
        return slot is not None and slot not in self._screen_tree.children

    def _footer_covered(self):
        """Whether a modal layer is over the footer (as its last update found it)."""
        return any(node._blocks_input for node in self._shown[1:])

    def header_changed(self):
        """The renderer's busy label changed: True when a page's slot draws the
        header, which this paint (or the one after the paint in progress) shows;
        False for content that draws its own header."""
        if self._page is None or self._content_chrome[0] is None:
            return False
        self.paint()
        return True

    def chrome_changed(self):
        """The footer's values changed: one transaction of the screen, whose frame
        is submitted; the screen's paint when the footer joins it. Inside a paint,
        or while the screen draws nothing (before its first frame, frozen
        output), the next paint draws it; under a modal layer, the paint that
        closes it."""
        screen = self._screen
        if (self.painting or screen is None or not self.renderer.render_allowed
                or self._footer_covered()):
            return False
        if self._footer_joins():
            # The screen's paint draws it whole.
            return self.paint()
        if self._held_footer() is None:
            return False
        screen.begin()
        try:
            self._place_footer()
        finally:
            # A footer that needs Python fails the frame as any node of the screen does.
            frame = screen.end(self.renderer)
        return bool(frame) and self.renderer.submit_frame(frame)

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

    def submit(self, commands, **metadata):
        """Admit a legacy page delta or repaint its surrounding composition (also
        when the footer joins the screen or has values to draw: a delta draws
        neither)."""
        if (self._rebuild or self.visible_layers
                or any(not layer.suspended for layer in self.layers)
                or not getattr(self.content, "accepts_deltas", False) or self._footer_joins()
                or (self._held_footer() is not None and self.renderer.footer_chrome.pending)):
            self.renderer.discard_surface()
            self.invalidate()
            return self.paint(receipt=metadata.get("receipt"),
                              receipt_phase=metadata.get("receipt_phase", "presented"))
        self.painting = True
        try:
            result = self.renderer.send(commands, **metadata)
            if result and self._screen is not None:
                # The delta's input is the content's until a frame paints the content.
                self._screen.note_written(CommandBatch(commands, BinaryCommand))
        finally:
            self.painting = False
        return self._paint_asked() or result
