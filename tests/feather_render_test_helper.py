## Semantic assertions for Feather renderer tests
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from contextlib import contextmanager
from dataclasses import dataclass
import shlex
from unittest import mock

from types import SimpleNamespace

from ui.engine import FrameLoop
from ui.renderer import MAX_ATOMIC_DRAW, MAX_BATCH_BYTES, FeatherRenderer


@dataclass(frozen=True)
class Bounds:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height


@dataclass(frozen=True)
class RenderedText:
    value: str
    x: int
    y: int
    font: str
    max_width: int | None
    max_height: int | None
    wrap: bool
    truncate: bool


@dataclass(frozen=True)
class RenderedShape:
    kind: str
    bounds: Bounds


@dataclass(frozen=True)
class RenderedButton:
    action: str
    bounds: Bounds
    label: str
    state: str
    font: str


@dataclass(frozen=True)
class RenderedToggle:
    action: str
    bounds: Bounds
    active: bool
    enabled: bool


def _option(tokens, name, count=1):
    try:
        start = tokens.index(name) + 1
    except ValueError:
        return None
    values = tokens[start:start + count]
    if len(values) != count:
        raise ValueError("render option %s is incomplete" % name)
    return values[0] if count == 1 else tuple(values)


def _bounds(tokens):
    position = _option(tokens, "-p", 2)
    size = _option(tokens, "-s", 2)
    if position is None or size is None:
        return None
    return Bounds(*(int(value) for value in position + size))


def _logical_action(value):
    prefix, separator, action = str(value).partition(":")
    return action if separator and prefix.isdigit() else str(value)


class RenderFrame:
    """One submitted Feather frame expressed as UI concepts, not CLI text."""

    def __init__(self, commands, renderer):
        self.texts = []
        self.shapes = []
        self.buttons = {}
        self.toggles = {}
        self.actions = set()
        self.hitboxes = renderer.hitboxes

        for command in commands:
            tokens = shlex.split(command)
            if len(tokens) < 2 or tokens[0] != "--batch":
                continue
            kind = tokens[1]
            action = _option(tokens, "--id")
            if action is not None:
                self.actions.add(_logical_action(action))
            if kind == "text":
                position = _option(tokens, "-p", 2)
                value = _option(tokens, "-t")
                font = _option(tokens, "-f")
                if position is None or value is None or font is None:
                    raise ValueError("incomplete rendered text command")
                max_width = _option(tokens, "--max-width")
                max_height = _option(tokens, "--max-height")
                self.texts.append(RenderedText(
                    value, int(position[0]), int(position[1]), font,
                    int(max_width) if max_width is not None else None,
                    int(max_height) if max_height is not None else None,
                    "--wrap" in tokens, "--truncate" in tokens))
            elif kind in ("fill", "stroke"):
                bounds = _bounds(tokens)
                if bounds is not None:
                    self.shapes.append(RenderedShape(kind, bounds))

        for action, spec in dict(renderer._buttons).items():
            self.buttons[action] = RenderedButton(
                action, Bounds(*spec[:4]), str(spec[4]), str(spec[5]),
                str(spec[6]))
        for action, spec in dict(renderer._toggles).items():
            self.toggles[action] = RenderedToggle(
                action, Bounds(*spec[:4]), bool(spec[4]), bool(spec[5]))
        self.actions.update(self.buttons)
        self.actions.update(self.toggles)
        self.actions.update(region.action for region in self.hitboxes)

    def has_action(self, action):
        return action in self.actions

    def button(self, action):
        try:
            return self.buttons[action]
        except KeyError as error:
            raise AssertionError("button is not rendered: %s" % action) from error

    def toggle(self, action):
        try:
            return self.toggles[action]
        except KeyError as error:
            raise AssertionError("toggle is not rendered: %s" % action) from error

    def text(self, value):
        matches = [text for text in self.texts if text.value == value]
        if len(matches) != 1:
            raise AssertionError(
                "expected one rendered text %r, found %d" %
                (value, len(matches)))
        return matches[0]

    def has_text(self, value):
        return any(text.value == value for text in self.texts)


class RenderCapture:
    """Accept every batch at the frame loop hand-off and record it.

    Everything before the hand-off runs for real: composition, frame-owner
    routing, surface acceptance, and visible-layer bookkeeping. Each batch is
    checked by an unstarted frame loop, which refuses what the running one
    refuses. ``submitted`` keeps each accepted ``RenderSubmission`` with its
    receipt and criticality; ``batches`` keeps just their command tuples;
    ``frames`` keeps the same submissions as semantic UI frames with the
    controls registered at submission time.
    """

    def __init__(self, renderer):
        self.submitted = []
        self.batches = []
        self.frames = []
        self._taken = 0
        self._renderer = renderer
        self._loop = FrameLoop(
            "/nonexistent/typer", "/nonexistent/events", "feather-test-no-such-process",
            max_frame=MAX_ATOMIC_DRAW, max_batch=MAX_BATCH_BYTES)
        renderer._submit = self._submit

    def _submit(self, submission):
        self._loop.submit(
            submission.commands, receipt=submission.receipt,
            phase=submission.receipt_phase, critical=submission.critical)
        self._loop.discard()
        self.submitted.append(submission)
        self.batches.append(submission.commands)
        self.frames.append(RenderFrame(submission.commands, self._renderer))
        return True

    def get(self):
        """The oldest submission not taken yet, or None."""
        if self._taken == len(self.submitted):
            return None
        self._taken += 1
        return self.submitted[self._taken - 1]

    @property
    def latest(self):
        if not self.frames:
            raise AssertionError("renderer has not submitted a frame")
        return self.frames[-1]


def page_screen(renderer, page="Home", deltas=False):
    """A full-screen root over the renderer showing page (a page's title, or a
    painter that sends what it shows), as the product's root shows its pages:
    the renderer's footer is a layer of its screen. deltas: the page takes the
    renderer's direct sends as its deltas. Not painted yet."""
    from feather.screen.composition import PaintSurface
    from ff5m_ui.keys import AppPage
    from ui import Rect, ScreenRoot
    from ui.renderer import SCREEN_HEIGHT, SCREEN_WIDTH
    painter = page if callable(page) else (lambda: renderer.send(renderer.begin_page(page)))
    surface = PaintSurface(painter, accepts_deltas=(lambda: True) if deltas else None)
    return ScreenRoot(renderer, surface, Rect(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT), AppPage.SCREEN_ROOT)


def shown_actions(root):
    """The logical actions of the regions Typer holds of root's screen."""
    shown = root.shown_input()
    return set() if shown is None else {region[0] for region in tuple(shown["base"]) + tuple(shown["overlay"])}


@contextmanager
def refused_render(renderer):
    """Refuse every batch at the frame loop hand-off, as a stopped loop does."""
    with mock.patch.object(renderer, "_submit", lambda submission: False):
        yield


def typer_events(decode, *lines):
    """Typer's event lines as FeatherRenderer.process_events() returns them:
    parsed by the engine's frame loop parser, with the action the loop would
    decode against the input it wrote given by decode(wire)."""
    taken = []
    for line in lines:
        event = FrameLoop.parse_event(line)
        if event[0] in ("tap", "touch", "button"):
            event = event[:-1] + (decode(event[1]),)
        taken.append(event)
    host = SimpleNamespace(_loop=SimpleNamespace(take=lambda: taken),
                           _sent_handler=None, _restart_handler=None)
    return FeatherRenderer.process_events(host, 0.0)
