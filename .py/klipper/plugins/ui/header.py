## The page header: a chrome tree in a page's slot of the frame owner's screen.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from dataclasses import dataclass

from .actions import Action, Back
from .bindings import bind, state
from .engine import Rect
from .identity import PageKey, StateKey
from .layout_helpers import FOOTER_Y, FRAME_WIDTH, FRAME_X, FRAME_Y, HEADER_BOTTOM, SCREEN_HEIGHT, SCREEN_WIDTH
from .theme import ThemeColor, ThemeRole

HEADER_BOUNDS = Rect(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT)
# The right slot: the header action, the busy notice or the page's header button, one at a time; their rects
# are in the slot.
RIGHT_SLOT = Rect(622, 7, 160, 46)
BUSY_NOTICE = Rect(0, 2, 160, 38)
HEADER_ACTION = Rect(26, 0, 132, 46)
BACK_BUTTON = Rect(14, 7, 146, 46)
# The title's width: narrower beside a back button or anything in the right slot.
TITLE_WIDTH, NARROW_TITLE_WIDTH = 740, 440


class HeaderPage(PageKey):
    __key_namespace__ = "feather.chrome"
    HEADER = "header"


class HeaderState(StateKey):
    """The header's values: the title as shown and which content the right slot shows."""

    __key_namespace__ = "feather.header"
    TITLE = state(str, default="")
    TITLE_WIDTH = state(int, default=TITLE_WIDTH)
    SHOWS_BACK = state(bool, default=False)
    BUSY_LABEL = state(str, default="")
    SHOWS_ACTION = state(bool, default=False)
    SHOWS_BUSY = state(bool, default=False)
    SHOWS_BUTTON = state(bool, default=False)


_SCHEMA = (HeaderState.TITLE, HeaderState.TITLE_WIDTH, HeaderState.SHOWS_BACK, HeaderState.BUSY_LABEL, HeaderState.SHOWS_ACTION,
           HeaderState.SHOWS_BUSY, HeaderState.SHOWS_BUTTON)


@dataclass(frozen=True)
class WireAction(Action):
    """An action the renderer names by its wire id: the wake region's, or a header action the product set by
    name."""

    wire: str
    kind = "wire"

    @property
    def wire_id(self):
        return self.wire

    def as_dict(self):
        return {"kind": self.kind, "wire": self.wire}


WAKE = WireAction("global.wake")


def _placed(node, rect):
    return node.offset(rect.x, rect.y).size(rect.width, rect.height)


def _action_button(header_action):
    from .components import Button
    action, label, button_state, font = header_action
    action = action if isinstance(action, Action) else WireAction(str(action))
    return _placed(Button(action, label, button_state, font=font), HEADER_ACTION)


def header_content(header_action, button):
    """The header's tree: the wake region, the panel above the footer, the back button (while it shows), the
    title and the right
    slot, which shows the header action, else the busy notice, else the page's header button (placed in the slot,
    RIGHT_SLOT)."""
    # The tree's modules load with the first page: a product's startup builds no declarative tree.
    from .components import Button, Fill, Hitbox, Stroke, Text
    from .layout import Overlay, When
    # The slot's fill covers it, so a change of what it shows repaints the slot alone.
    slot = [Fill(ThemeRole.HEADER_BACKGROUND).size(RIGHT_SLOT.width, RIGHT_SLOT.height),
            When(bind(HeaderState.SHOWS_BUSY), _placed(Overlay(
        Fill(ThemeRole.HEADER_BACKGROUND),
        Stroke(ThemeColor.WARNING, 2),
        Text(bind(HeaderState.BUSY_LABEL), ThemeColor.WARNING, "JetBrainsMono Bold 8pt", "center", "center",
             max_width=132, truncate=True)), BUSY_NOTICE))]
    if header_action is not None:
        slot.insert(1, When(bind(HeaderState.SHOWS_ACTION), _action_button(header_action)))
    if button is not None:
        slot.append(When(bind(HeaderState.SHOWS_BUTTON), button))
    title = Text(bind(HeaderState.TITLE), ThemeRole.HEADER_TEXT, "JetBrainsMono 12pt", "center", "center",
                 max_width=bind(HeaderState.TITLE_WIDTH), truncate=True)
    return Overlay(
        Hitbox(WAKE).size(SCREEN_WIDTH, SCREEN_HEIGHT),
        # Up to the footer, whose pixels the frame loop keeps.
        Fill(ThemeColor.BACKGROUND).size(SCREEN_WIDTH, FOOTER_Y - 2),
        Stroke(ThemeRole.HEADER_BORDER, 1).offset(FRAME_X, FRAME_Y).size(FRAME_WIDTH, FOOTER_Y - FRAME_Y - 1),
        Fill(ThemeRole.HEADER_BACKGROUND).offset(10, 6).size(780, HEADER_BOTTOM - 6),
        When(bind(HeaderState.SHOWS_BACK), _placed(Button(Back(), "< BACK", font="JetBrainsMono Bold 8pt"),
                                                   BACK_BUTTON)),
        title.offset(0, BACK_BUTTON.y).size(SCREEN_WIDTH, 44).repaint_boundary(),
        Fill(ThemeRole.HEADER_BORDER).offset(18, HEADER_BOTTOM).size(764, 1),
        Fill(ThemeColor.BORDER).offset(18, FOOTER_Y - 2).size(764, 1),
        _placed(Overlay(*slot), RIGHT_SLOT).repaint_boundary(),
    )


class Header:
    """A page slot's header: its tree and the values it shows.

    Title, back and the page's header button belong to the page the slot shows. The header action and the busy
    label are the renderer's (set_header_action, busy_notice), which imperative pages draw through begin_page too;
    the header reads them again in each transaction of its screen (apply). A changed header action or header
    button is a new tree, since it is new input; everything else is a value of the tree's state, so the screen
    keeps one header for every page it shows (show).
    """

    def __init__(self, title, back, button=None):
        self.title = str(title).upper()
        self.back = bool(back)
        self.button = button
        self._header_action = None
        self.page = self._create(None)

    def show(self, title, back, button=None):
        """Another page's title, back and header button."""
        self.title = str(title).upper()
        self.back = bool(back)
        if button is not self.button:
            self.button = button
            self.page = self._create(self._header_action)

    @property
    def node(self):
        return self.page.root

    def apply(self, renderer):
        """Inside the screen's transaction: the renderer's header action and busy label, shown by the slot's
        rule. True when the tree was made again (its node changed)."""
        header_action = getattr(renderer, "_header_action", None)
        busy = getattr(renderer, "_busy_label", None)
        made = header_action != self._header_action
        if made:
            self.page = self._create(header_action)
        shows_action = header_action is not None
        shows_busy = busy is not None and not shows_action
        shows_button = self.button is not None and not shows_action and not shows_busy
        narrow = self.back or shows_action or shows_busy or shows_button
        self.page.write({
            HeaderState.TITLE: self.title,
            HeaderState.TITLE_WIDTH: NARROW_TITLE_WIDTH if narrow else TITLE_WIDTH,
            HeaderState.SHOWS_BACK: self.back,
            HeaderState.BUSY_LABEL: "" if busy is None else str(busy),
            HeaderState.SHOWS_ACTION: shows_action,
            HeaderState.SHOWS_BUSY: shows_busy,
            HeaderState.SHOWS_BUTTON: shows_button,
        })
        return made

    def _create(self, header_action):
        from .layout import PageTree
        if self.button is not None and self.button.parent is not None:
            # The page's button moves into the new tree.
            self.button.parent = None
        self._header_action = header_action
        return PageTree(header_content(header_action, self.button), HEADER_BOUNDS,
                        page_id=HeaderPage.HEADER, state_schema=_SCHEMA)
