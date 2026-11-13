## The persistent footer: a chrome tree in the frame owner's screen, beside the page.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

# The tree's modules load with the first values: a product's startup builds no
# declarative tree.
from .bindings import bind, state
from .engine import Rect
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
    """The footer's tree, its values and whether it shows.

    The footer is a persisting layer of the frame owner's screen (ScreenRoot) beside the
    page: the composition decides when it draws. Its changes wait under a modal layer, it
    survives the frames that draw the page, and it is drawn again in full after a layer
    covered it or the panel's pixels went. A hidden footer (under a state screen, while
    output is frozen, with a new theme until the next page) is not in the screen and keeps
    only its values; the next page shows it with the latest ones. The tree is made with the
    first values, so nothing is drawn before them.

    content() makes the tree; state(left, right) gives the values it reads.
    """

    def __init__(self, content, state):
        self._content = content
        self._state = state
        self._page = None
        self._values = None
        self._written = None
        self.shown = False

    @property
    def page(self):
        """The footer's tree with its state; None before the first values."""
        return self._page

    @property
    def node(self):
        """The footer's root while it shows with values; None otherwise."""
        return self._page.root if self.shown and self._page is not None else None

    def write(self, values, owner=None):
        """Show values (left, right). owner is the frame owner whose screen holds the
        footer: a shown footer's change is one transaction of its screen."""
        changed = values != self._values
        self._values = values
        if self._page is None:
            self._create(values)
        if self.shown and owner is not None and changed:
            owner.chrome_changed()

    @property
    def pending(self):
        """Whether the tree has not taken the latest values yet."""
        return self._page is not None and self._values != self._written

    def apply(self):
        """Write the latest values into the tree; the owner's open transaction draws them."""
        if self._page is not None and self._values != self._written:
            self._written = self._values
            self._page.write(self._state(*self._values))

    def hide(self):
        """The footer's area belongs to something else: it leaves the screen."""
        self.shown = False

    def show(self):
        """A page is on the panel: the footer shows beside it, with its latest values."""
        self.shown = True

    def _create(self, values):
        from .layout import PageTree
        self._written = values
        self._page = PageTree(
            self._content(), FOOTER_BOUNDS, page_id=ChromePage.FOOTER,
            state_schema=(FooterState.LEFT, FooterState.RIGHT), state=self._state(*values))
