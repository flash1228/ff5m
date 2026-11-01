## The persistent footer: a chrome tree the frame loop draws off the reactor.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

# The tree's modules load with the first values: a product's startup builds no
# declarative tree.
from .bindings import bind, state
from .engine import Rect, ScreenFrames
from .identity import PageKey, StateKey
from .layout_helpers import FOOTER_HEIGHT, FOOTER_Y, FRAME_WIDTH, FRAME_X, SCREEN_HEIGHT, SCREEN_WIDTH
from .theme import ThemeColor

# The footer's strip: the separator row above the panel down to the screen's bottom.
FOOTER_BOUNDS = Rect(0, FOOTER_Y - 2, SCREEN_WIDTH, SCREEN_HEIGHT - (FOOTER_Y - 2))


class ChromePage(PageKey):
    __key_namespace__ = "feather.chrome"
    FOOTER = "footer"


class FooterState(StateKey):
    """The footer's values; a product's footer content reads them."""

    __key_namespace__ = "feather.footer"
    LEFT = state(str, default="")
    RIGHT = state(str, default="")


def footer_panel(*content):
    """The footer's panel in FOOTER_BOUNDS with content over it.

    The strip is cleared in full first, so startup overlays and previous themes cannot
    survive in the outer gutters or bottom rows."""
    from .components import Fill, Stroke
    from .layout import Overlay
    return Overlay(
        Fill(ThemeColor.BACKGROUND),
        Fill(ThemeColor.PANEL).offset(10, 2).size(SCREEN_WIDTH - 20, FOOTER_HEIGHT - 1),
        Stroke(ThemeColor.BORDER, 1).offset(FRAME_X, 0).size(FRAME_WIDTH, FOOTER_BOUNDS.height - 4),
        *content)


def footer_texts():
    """The framework's footer texts on the panel: FooterState.LEFT and RIGHT."""
    from .components import Text
    font = "JetBrainsMono 8pt"
    return (
        Text(bind(FooterState.LEFT), ThemeColor.PRIMARY, font, "left",
             max_width=380, truncate=True).offset(20, 2).size(380, FOOTER_HEIGHT),
        Text(bind(FooterState.RIGHT), ThemeColor.PRIMARY, font, "right",
             max_width=340, truncate=True).offset(440, 2).size(340, FOOTER_HEIGHT),
    )


def footer_content():
    """The framework's footer: the left and right texts on the panel."""
    return footer_panel(*footer_texts())


class Footer:
    """The footer's tree, its values and whether it is held.

    A held footer draws nothing: its pixels are covered (a dialog, a modal state screen,
    frozen output) or stale (another theme) until the next page shows it, which draws it
    in full with the current values. The tree is made with the first values, so nothing
    is drawn before them; until then the footer keeps the hold itself.

    content() makes the tree; state(left, right) gives the values it reads.

    Each change is one transaction of the footer's ScreenFrames. A frame loop it is
    attached to draws it on its own thread; without one the transaction's end builds the
    frame here and the footer hands it to submit.
    """

    def __init__(self, content, state, renderer, submit):
        self._content = content
        self._state = state
        self._renderer = renderer
        self._submit = submit
        self._page = None
        self._screen = None
        self._loop = None
        self._held = False

    @property
    def page(self):
        """The footer's tree with its state; None before the first values."""
        return self._page

    def attach(self, loop):
        """Let loop draw the footer; None: each transaction builds its own frame (no
        loop runs). A loop draws it in full at its next frame: what it showed before
        is gone (the panel was cleared)."""
        previous, self._loop = self._loop, loop
        if self._screen is None:
            return
        if previous is not None:
            previous.attach(None)
        if loop is not None:
            loop.attach(self._screen)
            self._change(redraw=True)

    def write(self, values, covered=False):
        """Show values (left, right); covered: they wait under a cover, as hold()."""
        if self._screen is None:
            self._held = self._held or covered
            self._create(values)
            self._change(held=self._held)
            return
        state = self._page.state
        changed = (state.get(FooterState.LEFT), state.get(FooterState.RIGHT)) != values
        if changed or (covered and not self._screen.held):
            self._change(values if changed else None, True if covered else None)

    def hold(self):
        """The footer's pixels are covered: draw nothing until the next page shows it."""
        if self._screen is None:
            self._held = True
        elif not self._screen.held:
            self._change(held=True)

    def show(self):
        """A page is on the panel: the footer is visible, and drawn in full if it was
        held."""
        if self._screen is None:
            self._held = False
        else:
            self._change(held=False)

    def _change(self, values=None, held=None, redraw=False):
        screen = self._screen
        screen.begin()
        try:
            if held is not None:
                screen.hold(held)
            if redraw:
                screen.redraw()
            if values is not None:
                self._page.write(self._state(*values))
        finally:
            try:
                frame = screen.end(self._renderer)
            except RuntimeError as error:
                # As an attached loop reports it; the tree is left clean. The renderer
                # imports this module, so its report is imported here.
                from .renderer import screen_failed
                screen_failed(str(error))
                frame = None
        if frame:
            self._submit(frame)

    def _create(self, values):
        from .layout import PageTree
        self._page = PageTree(
            self._content(), FOOTER_BOUNDS, page_id=ChromePage.FOOTER,
            state_schema=(FooterState.LEFT, FooterState.RIGHT), state=self._state(*values))
        self._screen = ScreenFrames(self._page.root, self._page.state, FOOTER_BOUNDS)
        if self._loop is not None:
            self._loop.attach(self._screen)
