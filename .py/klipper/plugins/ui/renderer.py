## Low-level Typer renderer for Feather UI object trees.
##
## This module deliberately contains no Klipper state or business logic. The
## declarative layout engine and components live next to it in the ui package.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import logging
import math
from collections import namedtuple
from contextlib import contextmanager

from .actions import Action, DismissToast, action_wire_id
from .engine import (
    NATIVE_METHODS, CommandBatch, CommandList, FrameLoop, RendererBase, dialog_layout, quote, renderer_normalize_font,
    renderer_normalize_font_for_text, renderer_text_width,
)
from .font_metrics import (
    get_font_metrics, load_runtime_metrics, set_font_metrics,
)
from .layout_helpers import (
    DIALOG_BODY_FONT, DIALOG_BODY_HALF_HEIGHT, DIALOG_BODY_Y, DIALOG_BUTTON_BOTTOM,
    DIALOG_BUTTON_COMPACT_FONT, DIALOG_BUTTON_FONT, DIALOG_BUTTON_MEDIUM_FONT,
    DIALOG_MIN_HEIGHT, DIALOG_MIN_WIDTH,
    DIALOG_BUTTON_HEIGHT, DIALOG_BUTTON_GAP, DIALOG_COMPACT_BUTTON_BOTTOM,
    DIALOG_COMPACT_GAP, DIALOG_COMPACT_TITLE_TOP, DIALOG_GROUP_BUTTON_BOTTOM,
    DIALOG_LINE_SPACING, DIALOG_MULTILINE_GAP, DIALOG_ONE_LINE_GAP,
    DIALOG_PAGER_MIN_HEIGHT, DIALOG_PAGER_RESERVE, DIALOG_TITLE_FONT,
    DIALOG_TITLE_LINE_SPACING, DIALOG_TITLE_TOP, DIALOG_TOP, DIALOG_BOTTOM_INSET,
    DIALOG_STATUS_FONT, DIALOG_STATUS_GAP,
    dialog_vertical_bounds, dialog_pager_bounds,
)
from .numeric_input import NumericInputSpec
from .render_receipts import validate_render_receipt_token
from .theme import ThemeColor, ThemeRole, resolve_theme
from .theme_catalog import (
    DEFAULT_THEME, FALLBACK_THEME, THEME_DIRECTORY, USER_THEME_DIRECTORY,
    ThemeCatalog, normalize_theme_name,
)


Hitbox = namedtuple("Hitbox", "action x y width height continuous layer")
# One batch for the frame loop; a critical batch is written again to a restarted Typer.
RenderSubmission = namedtuple("RenderSubmission", "commands receipt receipt_phase critical")

# The fonts and constants of the engine's dialog layout, in dialog_layout's order; the renderer's paddings and
# the screen size follow them.
_DIALOG_POLICY = (
    DIALOG_TITLE_FONT, DIALOG_BODY_FONT, DIALOG_BUTTON_FONT, DIALOG_BUTTON_MEDIUM_FONT, DIALOG_BUTTON_COMPACT_FONT,
    DIALOG_TOP, DIALOG_BOTTOM_INSET, DIALOG_MIN_WIDTH, DIALOG_MIN_HEIGHT, DIALOG_TITLE_TOP, DIALOG_BODY_Y,
    DIALOG_LINE_SPACING, DIALOG_BODY_HALF_HEIGHT, DIALOG_PAGER_RESERVE, DIALOG_TITLE_LINE_SPACING,
    DIALOG_BUTTON_HEIGHT, DIALOG_BUTTON_GAP, DIALOG_BUTTON_BOTTOM, DIALOG_ONE_LINE_GAP, DIALOG_MULTILINE_GAP,
    DIALOG_GROUP_BUTTON_BOTTOM, DIALOG_COMPACT_TITLE_TOP, DIALOG_COMPACT_GAP, DIALOG_COMPACT_BUTTON_BOTTOM,
    DIALOG_PAGER_MIN_HEIGHT,
)


DRAW_PIPE = "/tmp/typer"
EVENT_PIPE = "/tmp/feather-events"
TOUCH_DEVICE = "/dev/input/guppy"
TYPER_BINARY = "/root/printer_data/bin/typer"
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 480
FRAME_X = 8
FRAME_Y = 4
FRAME_WIDTH = 784
FRAME_HEIGHT = 472
HEADER_BOTTOM = 55
FOOTER_Y = 444
FOOTER_HEIGHT = 32
CONTENT_BOTTOM = FOOTER_Y - 2


class PageChromeContract:
    """Framework-owned geometry of the page chrome painted around content.

    ``begin_page`` paints a header above ``HEADER_BOTTOM`` and a persistent
    footer below ``CONTENT_BOTTOM``. Authoring tools place new page content
    inside ``content_bounds`` so that it does not overlap that chrome.
    """

    __slots__ = ("strategy", "content_insets")

    def __init__(self, strategy, content_insets):
        self.strategy = str(strategy)
        left, top, right, bottom = (int(value) for value in content_insets)
        if min(left, top, right, bottom) < 0:
            raise ValueError("Page content insets must be non-negative")
        self.content_insets = (left, top, right, bottom)

    def content_bounds(self, width=SCREEN_WIDTH, height=SCREEN_HEIGHT):
        left, top, right, bottom = self.content_insets
        width, height = int(width), int(height)
        if width - left - right <= 0 or height - top - bottom <= 0:
            raise ValueError("Viewport is smaller than the page chrome")
        return (left, top, width - left - right, height - top - bottom)

    def as_dict(self, width=SCREEN_WIDTH, height=SCREEN_HEIGHT):
        return {
            "strategy": self.strategy,
            "content_insets": list(self.content_insets),
            "content_bounds": list(self.content_bounds(width, height)),
        }


PAGE_CHROME_CONTRACT = PageChromeContract(
    "core.page_chrome",
    (12, HEADER_BOTTOM + 9, 12, SCREEN_HEIGHT - CONTENT_BOTTOM + 4))
BUSY_NOTICE_X = 622
BUSY_NOTICE_Y = 9
BUSY_NOTICE_WIDTH = 160
BUSY_NOTICE_HEIGHT = 38
# What one send() may carry: one transaction must hold the largest frame the
# screen shows, a full 800x480 picture at 4 bits per pixel (192,000 bytes, as
# bands of at most MAX_ATOMIC_DRAW) with a page around it. It only checks a
# batch: nothing is allocated for it, and the frame loop writes a transaction
# one frame at a time.
MAX_BATCH_BYTES = 256 * 1024
MAX_PENDING_DRAW = MAX_BATCH_BYTES
# Historical public name retained for compatibility.  This is a bounded
# logical transport-frame size, not a Linux FIFO atomic-write guarantee:
# 8 KiB may exceed PIPE_BUF.  Correctness depends on the frame loop being the
# sole draw-FIFO writer and completing partial writes.
MAX_ATOMIC_DRAW = 8 * 1024
PRESENT_GUARD_US = 3000

_ButtonSpec = namedtuple(
    "ButtonSpec",
    ("x", "y", "width", "height", "label", "state", "font",
     "subtitle", "layout", "subtitle_font", "subtitle_color", "accent",
     "surfaces"),
)


class BinaryCommand(str):
    """A Typer command whose payload follows its transport header."""

    __slots__ = ("payload",)

    def __new__(cls, command, payload):
        if not isinstance(payload, bytes):
            raise TypeError("binary command payload must be immutable bytes")
        value = str.__new__(cls, command)
        value.payload = payload
        return value


def _immutable_commands(commands):
    """Freeze a batch: binary commands as they are, everything else as text."""
    if isinstance(commands, CommandBatch):
        return commands
    return CommandBatch(commands, BinaryCommand)


_NATIVE = NATIVE_METHODS["renderer"]


class FeatherRenderer(RendererBase):
    """Translate small UI primitives into typer display-list commands."""

    # The engine draws the primitives. They read the renderer's palette,
    # surface, button and hitbox state, and call each other through the
    # instance, so a subclass or instance attribute may replace any of them.
    color = _NATIVE["color"]
    fill = _NATIVE["fill"]
    stroke = _NATIVE["stroke"]
    panel = _NATIVE["panel"]
    text = _NATIVE["text"]
    composite_button = _NATIVE["composite_button"]
    button = _NATIVE["button"]
    buttons = _NATIVE["buttons"]
    # Button layouts other than a centred label, and the geometric up/down
    # arrow button.
    _button_colors = _NATIVE["_button_colors"]
    _button_commands = _NATIVE["_button_commands"]
    arrow_button = _NATIVE["arrow_button"]
    _arrow_button_commands = _NATIVE["_arrow_button_commands"]
    _wire_action = _NATIVE["_wire_action"]
    # Input regions: action_hitbox registers one and returns its command,
    # button_hitbox skips inactive buttons, and redraw_page_hitboxes replays
    # the page chrome's regions without painting.
    _hitbox = _NATIVE["_hitbox"]
    clear_hitboxes = _NATIVE["clear_hitboxes"]
    action_hitbox = _NATIVE["action_hitbox"]
    button_hitbox = _NATIVE["button_hitbox"]
    _wake_hitbox = _NATIVE["_wake_hitbox"]
    redraw_page_hitboxes = _NATIVE["redraw_page_hitboxes"]
    # Framework values the native primitives use.
    _action_type = Action
    _button_spec = _ButtonSpec
    _literal_types = (ThemeColor, ThemeRole)
    _font_metrics = staticmethod(get_font_metrics)
    _default_background = ThemeColor.BACKGROUND
    _default_foreground = ThemeColor.PRIMARY
    _default_panel = ThemeColor.PANEL
    _default_subtitle = ThemeColor.DIM
    # Page chrome bounds. The wake region covers the screen and is registered
    # before every control, which Typer gives precedence where they overlap.
    _wake_bounds = (0, 0, SCREEN_WIDTH, SCREEN_HEIGHT)
    _back_bounds = (14, 7, 146, 46)
    _header_action_bounds = (648, 7, 132, 46)

    BUTTON_COLORS = {
        "primary": (ThemeRole.ACCENT_BACKGROUND, ThemeRole.ACCENT_BORDER,
                    ThemeRole.ACCENT_TEXT),
        "enabled": (ThemeRole.BUTTON_BACKGROUND, ThemeRole.BUTTON_BORDER,
                    ThemeRole.BUTTON_TEXT),
        "disabled": (ThemeRole.BUTTON_BACKGROUND, ThemeColor.MUTED,
                     ThemeColor.DIM),
        "selected": (
            ThemeRole.BUTTON_SELECTED_BACKGROUND,
            ThemeRole.BUTTON_SELECTED_BORDER,
            ThemeRole.BUTTON_SELECTED_TEXT),
        "warning": (ThemeRole.BUTTON_BACKGROUND, ThemeColor.WARNING,
                    ThemeColor.WARNING),
        "danger": (ThemeColor.DANGER_BACKGROUND, ThemeColor.DANGER, ThemeColor.DANGER),
        "busy": (ThemeRole.BUTTON_BACKGROUND, ThemeColor.WARNING,
                 ThemeColor.WARNING),
        "pressed": (ThemeColor.PRESSED_BACKGROUND, ThemeColor.BRIGHT, ThemeColor.BRIGHT),
        "keypad": (ThemeRole.ACCENT_BACKGROUND, ThemeRole.ACCENT_BORDER,
                   ThemeRole.ACCENT_TEXT),
        "keypad_aux": (ThemeColor.PANEL, ThemeColor.SECONDARY,
                       ThemeColor.SECONDARY),
        "keypad_confirm": (
            ThemeRole.ACCENT_BACKGROUND, ThemeRole.ACCENT_BORDER,
            ThemeRole.ACCENT_TEXT),
    }
    BUTTON_TEXT_PADDING = 16
    HINT_TEXT_PADDING = 20
    DIALOG_TEXT_PADDING = 28
    def __init__(self, debug=False, theme_directories=None, blending=True,
                 raster_acceleration="scalar", ram_composition=False):
        self.debug = debug
        self.ram_composition = bool(ram_composition)
        self.blending = bool(blending)
        self.raster_acceleration = str(raster_acceleration).strip().lower()
        if self.raster_acceleration not in ("scalar", "neon"):
            raise ValueError("invalid raster acceleration: %s" %
                             raster_acceleration)
        self._theme_directories = tuple(
            theme_directories or (THEME_DIRECTORY, USER_THEME_DIRECTORY))
        self._theme_catalog = ThemeCatalog.from_directories(
            self._theme_directories)
        self._themes = {}
        self._theme_descriptions = {}
        self._user_theme_issues = ()
        self._theme_name = DEFAULT_THEME
        self._palette = resolve_theme(FALLBACK_THEME)
        self.reload_themes()
        self.event_fd = None
        self._last_footer = None
        self._footer_values = None
        self._footer_drawn = False
        self._reset_interactions()
        self._generation = 0
        # The native frame loop that owns Typer; made by the first start().
        self._loop = None
        self._sent_handler = None
        self._event_fd_handler = None
        self._restart_handler = None
        self._next_batch_kind = None
        self._next_batch_key = None
        self._composite_commands = None
        self._composite_kind = None
        self._composite_key = None
        self._composite_receipt = None
        self._busy_label = None
        self._header_action = None
        self._menu_suppressed = False
        self._loader_active = False
        self._output_frozen = False
        self._font_manifest_loaded = False
        self._semantic_page_id = None
        self._page_title = ""
        self._page_back = False
        self._prepared_surface = None
        self._frame_owner = None
        self._preserve_input = False
        self._retain_background = False
        self._dialog_bounds = ()

    def configure_frame_owner(self, owner):
        """Route every ordinary submission through the screen composition root."""
        self._frame_owner = owner

    # The engine keeps the input (generation, buttons, regions, pressed
    # buttons and the menu button's suppression) with its own snapshot; these
    # are the other surface fields.
    _SURFACE_FIELDS = (
        "_toggles", "_page_title", "_page_back", "_semantic_page_id",
        "_loader_active", "_footer_drawn", "_last_footer",
        "_header_action", "_dialog_bounds",
    )
    # Surface fields mutated in place; the snapshot copies them. Every other
    # field is replaced on change, so the snapshot shares its value.
    _SURFACE_CONTAINERS = ("_toggles",)

    @property
    def render_allowed(self):
        return not self._output_frozen

    # Whether the renderer may draw; the first call of a surface keeps the
    # visible surface fields and snapshots the input.
    _prepare_surface = _NATIVE["_prepare_surface"]

    def _finish_surface(self, accepted):
        previous = self._prepared_surface
        self._prepared_surface = None
        if previous is None:
            return
        if accepted:
            self._drop_input_snapshot()
            return
        for name, value in previous.items():
            setattr(self, name, value)
        self._restore_input()

    def discard_surface(self):
        """Roll back a prepared surface that was not submitted."""
        self._finish_surface(False)

    def _visible_value(self, name):
        if self._prepared_surface is not None:
            return self._prepared_surface[name]
        return getattr(self, name)

    def configure_worker(self, event_fd_handler, restart_handler=None):
        """Connect the reactor before start().

        event_fd_handler(old_fd, new_fd) registers the descriptor that is
        readable while process_events() has work (new_fd None: unregister
        old_fd); restart_handler() repaints after Typer started again.
        """
        self._event_fd_handler = event_fd_handler
        self._restart_handler = restart_handler

    @property
    def active(self):
        return self._loop is not None and self._loop.running

    @property
    def output_frozen(self):
        return self._output_frozen

    @property
    def touch_warning_allowed(self):
        """Whether the visible surface has controls worth protecting."""
        actions = set(self._visible_input("_buttons"))
        actions.update(action for action, _spec in self._visible_input("_hitboxes")
                       if action != "global.wake")
        return not self._visible_value("_loader_active") and bool(actions)

    def discard_pending_output(self):
        """Drop every unwritten batch before a final lifecycle screen."""
        if self._loop is not None:
            self._loop.discard()

    def freeze_output(self):
        """Keep the last submitted safety screen as the sole display owner."""
        self._output_frozen = True

    def thaw_output(self):
        self._output_frozen = False

    def _clear_display(self):
        """Replace every framebuffer pixel before this renderer draws. It
        precedes whatever the frame owner composes, so it is not the owner's."""
        self._footer_drawn = False
        return self._submit(RenderSubmission(_immutable_commands([
            self.clear_hitboxes("base"),
            self.clear_hitboxes("overlay"),
            "--batch clear -c %s" % self.color(ThemeColor.BACKGROUND),
        ]), None, "presented", True))

    # str(value) as one double-quoted Typer token: line breaks become "\n", a
    # line holding only the frame sentinel "--end" gets a leading space, and
    # backslashes and quotes are escaped.
    quote = staticmethod(quote)

    def start(self):
        """Start the frame loop, which launches Typer on its own thread.

        Until then batches are admitted without being written, so a screen
        that another owner holds (a boot screen) is a renderer not started
        yet: start() is its release.
        """
        self._output_frozen = False
        if self._event_fd_handler is None:
            raise RuntimeError("frame loop callbacks are not configured")
        if self.active:
            return False
        if not self._font_manifest_loaded:
            set_font_metrics(load_runtime_metrics(TYPER_BINARY))
            self._font_manifest_loaded = True
        if self._loop is None:
            self._loop = FrameLoop(
                DRAW_PIPE, EVENT_PIPE, "typer", max_frame=MAX_ATOMIC_DRAW,
                max_batch=MAX_BATCH_BYTES)
        started = self._loop.start(self._typer_arguments())
        self.event_fd = self._loop.fileno()
        self._event_fd_handler(None, self.event_fd)
        self._busy_label = None
        self._last_footer = None
        self._footer_drawn = False
        # Typer's double buffer initially contains whatever was left in the
        # framebuffer. Clear the complete panel before the first partial page
        # render so neither the persistent footer nor the outer margins can
        # expose pixels from the previous screen owner. Whatever the frame
        # owner composed before is not on the panel, so it draws all of it.
        self._clear_display()
        if self._frame_owner is not None:
            self._frame_owner.surface_cleared()
        return started

    def _typer_arguments(self):
        arguments = [TYPER_BINARY]
        if self.debug:
            arguments.append("--debug")
        arguments += ["--deferred-page-publish", "auto",
                      "--present-guard-us", str(PRESENT_GUARD_US)]
        if self.blending:
            arguments.append("--blending")
        if self.raster_acceleration != "scalar":
            arguments += ["--raster-acceleration", self.raster_acceleration]
        if self.ram_composition:
            # Opt-in: Typer composes in an extra 1.5 MB RAM image, trading
            # scarce printer memory for fewer uncached framebuffer reads.
            arguments.append("--ram-composition")
        arguments += ["--double-buffered", "--touch-device", TOUCH_DEVICE,
                      "--event-pipe", EVENT_PIPE, "batch", "--pipe", DRAW_PIPE]
        return arguments

    def stop(self):
        """Write what is pending, stop the frame loop and leave Typer showing the last screen."""
        if self.active:
            self._loop.stop()
            self._event_fd_handler(self.event_fd, None)
            self.event_fd = None
        self._busy_label = None
        self._last_footer = None
        self._footer_drawn = False
        self._output_frozen = False

    def restart(self):
        return self._loop is not None and self._loop.restart()

    def set_sent_handler(self, callback):
        """Notify on the reactor after a receipt-bearing batch is fully written.

        This is transport completion, not render completion or presentation.
        Consumers must ignore tokens from cancelled or superseded operations.
        """
        self._sent_handler = callback

    def process_events(self, eventtime):
        """On the reactor, once event_fd is readable: deliver the frame loop's
        sent and restart notices and return Typer's lines in order."""
        lines = []
        for event in self._loop.take():
            if event[0] == "line":
                lines.append(event[1])
            elif event[0] == "sent":
                if self._sent_handler is not None:
                    self._sent_handler(event[1], eventtime)
            else:
                logging.warning("[feather_screen] typer restarted (last error: %s)",
                                self._loop.status()["last_error"] or "none")
                if self._restart_handler is not None:
                    self._restart_handler()
        return lines

    @contextmanager
    def compose(self, preserve_input=False, retain_background=False):
        """Submit consecutive ordinary draws as one admitted frame."""
        if self._composite_commands is not None:
            raise RuntimeError("render composition is already active")
        self._composite_commands = CommandList()
        self._composite_kind = None
        self._composite_key = None
        self._composite_receipt = None
        self._preserve_input = bool(preserve_input)
        self._retain_background = bool(retain_background)
        self._prepare_surface()
        self._dialog_bounds = ()
        frame = {"accepted": False, "input_changed": False, "background_changed": False}
        try:
            yield frame
        except BaseException:
            self._composite_commands = None
            self._composite_kind = None
            self._composite_key = None
            self._composite_receipt = None
            self._preserve_input = False
            self._retain_background = False
            self._finish_surface(False)
            raise
        commands = self._composite_commands
        kind = self._composite_kind
        key = self._composite_key
        receipt = self._composite_receipt
        self._composite_commands = None
        self._composite_kind = None
        self._composite_key = None
        self._composite_receipt = None
        self._preserve_input = False
        self._retain_background = False
        previous = self._prepared_surface
        if (commands and retain_background and previous is not None
                and (not self._dialog_bounds
                     or self._dialog_bounds != previous["_dialog_bounds"]
                     or self._header_action != previous["_header_action"])):
            frame["background_changed"] = True
            requested_header = self._header_action
            self._finish_surface(False)
            self._prepare_surface()
            self._header_action = requested_header
            return
        if commands and preserve_input and previous is not None:
            # Replace only input IDs after building pixels once. These final
            # resets remove IDs embedded in the earlier draw commands.
            replaced = self._finish_kept_input()
            if replaced:
                frame["input_changed"] = True
                # Input replacement is a prerequisite for later pixel-only updates.
                key = None
                commands.extend(replaced)
                if kind is None:
                    kind = "state"
        if commands:
            if receipt is None:
                frame["accepted"] = self.send(commands, kind=kind, key=key)
            else:
                frame["accepted"] = self.send(
                    commands, kind=kind, key=key, receipt=receipt[0], receipt_phase=receipt[1])
        else:
            self._finish_surface(False)

    @property
    def header_action_changed(self):
        """Whether a staged header control needs its old pixels restored."""
        return (self._prepared_surface is not None
                and self._header_action != self._prepared_surface["_header_action"])

    @property
    def retaining_background(self):
        """The current foreground pass must leave retained backdrop pixels intact."""
        return self._retain_background

    @contextmanager
    def collect(self):
        """Adapt an imperative painter into one node's ordered command list."""
        if self._composite_commands is None:
            raise RuntimeError("command collection requires a composed frame")
        previous = self._composite_commands
        commands = self._composite_commands = []
        try:
            yield commands
        finally:
            self._composite_commands = previous

    def send(self, commands, kind=None, key=None, generation=None,
             receipt=None, button_feedback=False, receipt_phase="presented"):
        """Publish one immutable batch; registered buttons can respond while frozen."""
        if self._output_frozen and not button_feedback:
            self._finish_surface(False)
            return False
        if not commands:
            if self._composite_commands is None and not button_feedback:
                self._finish_surface(False)
            return False
        if receipt_phase != "presented" and (receipt_phase != "staged" or receipt is None):
            raise ValueError("staged receipts require a token; unknown receipt phase")
        immutable = _immutable_commands(commands)
        if kind is None and self._next_batch_kind is not None:
            kind, key = self._next_batch_kind, self._next_batch_key
        self._next_batch_kind = None
        self._next_batch_key = None
        if self._composite_commands is not None:
            if generation is not None or button_feedback:
                raise ValueError("this render batch cannot be composed")
            if receipt is not None:
                token = validate_render_receipt_token(receipt)
                request = (token, receipt_phase)
                if self._composite_receipt not in (None, request):
                    raise ValueError("a composed frame can have only one receipt and phase")
                self._composite_receipt = request
            self._composite_commands.extend(immutable)
            if (kind == "critical"
                    or (kind == "surface" and self._composite_kind != "critical")
                    or (kind == "state" and self._composite_kind is None)):
                self._composite_kind = kind
                self._composite_key = key
            return True
        owner = self._frame_owner
        if owner is not None and not owner.painting and not button_feedback:
            if receipt_phase != "presented":
                return owner.submit(
                    immutable, kind=kind, key=key, generation=generation,
                    receipt=receipt, receipt_phase=receipt_phase)
            return owner.submit(
                immutable, kind=kind, key=key, generation=generation,
                receipt=receipt)
        if kind not in (None, "critical", "surface", "state", "animation"):
            self._finish_surface(False)
            raise ValueError("unknown render batch kind: %s" % kind)
        try:
            receipt = (None if receipt is None else
                       validate_render_receipt_token(receipt))
            accepted = self._submit(RenderSubmission(
                immutable, receipt, receipt_phase, kind == "critical"))
        except ValueError as exc:
            logging.warning("[feather_screen] rejected render batch: %s", exc)
            accepted = False
        except BaseException:
            if not button_feedback:
                self._finish_surface(False)
            raise
        if not button_feedback:
            self._finish_surface(accepted)
        return accepted

    def _submit(self, submission):
        """Hand a batch to the frame loop. Before the first start() Typer does
        not run and start() clears the whole panel, so the batch is admitted
        without being written; after stop() it is refused."""
        if self._loop is None:
            return True
        if not self._loop.running:
            return False
        return self._loop.submit(
            submission.commands, receipt=submission.receipt,
            phase=submission.receipt_phase, critical=submission.critical)

    def prioritize_next_batch(self, kind, key=None):
        """Classify the following legacy one-argument send() call."""
        self._next_batch_kind = kind
        self._next_batch_key = key

    def send_animation(self, commands, key, receipt=None):
        return self.send(
            commands, kind="animation", key=key, receipt=receipt)

    def request_presented_receipt(self, token):
        """Queue a Typer presentation barrier without repainting the frame."""
        if not self.render_allowed:
            return False
        token = validate_render_receipt_token(token)
        # A flush alone: a protocol-only barrier passes the modal frame owner.
        return self._submit(RenderSubmission(
            _immutable_commands(()), token, "presented", False))

    def get_status(self):
        loop = (self._loop.status() if self._loop is not None else
                {"state": "stopped", "last_error": "", "restarts": 0,
                 "submissions": 0, "transactions": 0, "pending": 0,
                 "in_flight": 0, "dropped_events": 0})
        return {
            "worker_state": loop["state"],
            "worker_last_error": loop["last_error"],
            "typer_restarts": loop["restarts"],
            "submitted_batches": loop["submissions"],
            "transactions": loop["transactions"],
            "pending_batches": loop["pending"],
            "in_flight": loop["in_flight"],
            "dropped_events": loop["dropped_events"],
            "semantic_page_id": self._semantic_page_id,
        }

    set_semantic_page = _NATIVE["set_semantic_page"]

    # decode_action(action) is the engine's: the logical action of a tap when
    # its generation is the visible surface's, so taps emitted for a page that
    # has already been replaced are refused (None).

    @property
    def generation(self):
        return self._visible_input("_generation")

    def invalidate_input_generation(self):
        """Prepare a new input generation for a replacement surface."""
        if self._prepare_surface() and not self._preserve_input:
            self._generation += 1

    @property
    def theme_name(self):
        return self._theme_name

    def _sync_theme_catalog(self):
        previous_name = self._theme_name
        previous_palette = self._palette
        self._themes = self._theme_catalog.themes
        self._theme_descriptions = self._theme_catalog.descriptions
        self._user_theme_issues = self._theme_catalog.user_issues
        if self._theme_name not in self._themes:
            self._theme_name = DEFAULT_THEME
        self._palette = self._themes[self._theme_name]
        if (self._theme_name != previous_name
                or self._palette != previous_palette):
            self._last_footer = None
            self._footer_drawn = False
        return tuple(self._themes)

    def reload_themes(self):
        """Reload the complete catalog at construction and klippy:ready."""
        self._theme_catalog.reload_all()
        return self._sync_theme_catalog()

    def reload_user_themes(self):
        """Refresh writable user themes without rescanning bundled files."""
        self._theme_catalog.reload_user_themes()
        return self._sync_theme_catalog()

    def ensure_user_theme_directory(self):
        self._theme_catalog.ensure_user_directories()

    def theme_names(self, reload=False):
        # ``reload=True`` is retained for callers outside this package. A UI
        # refresh must never rescan immutable bundled themes.
        if reload:
            self.reload_user_themes()
        return tuple(self._themes)

    def theme_description(self, name):
        return self._theme_catalog.description(name)

    def user_theme_issues(self):
        return tuple(self._user_theme_issues)

    def set_theme(self, name):
        normalized = normalize_theme_name(name)
        if normalized not in self._themes:
            logging.warning("[feather_screen] unknown theme %r; using DEFAULT",
                            name)
            normalized = DEFAULT_THEME
        changed = normalized != self._theme_name
        self._theme_name = normalized
        self._palette = self._themes[normalized]
        if changed:
            # The footer normally survives page redraws and is skipped while
            # its values stay unchanged. A palette change is also a content
            # change, so force begin_page() to repaint it in the new colors.
            self._last_footer = None
            self._footer_drawn = False
        return changed

    def modal_scrim(self):
        """Darken the retained framebuffer with 70%-opaque black."""
        return "--batch fill -p 0 0 -s %d %d -c B3000000" % (
            SCREEN_WIDTH, SCREEN_HEIGHT)

    def image(self, x, y, blob, format="fxi1"):
        if format != "fxi1":
            raise ValueError("unsupported image format: %s" % format)
        return BinaryCommand(
            "--batch image -p %d %d --format fxi1" % (int(x), int(y)),
            blob)

    def filled_circle(self, center_x, center_y, radius, color=ThemeColor.PRIMARY):
        """Draw a compact filled circle using horizontal one-pixel spans."""
        radius = max(1, int(radius))
        commands = []
        for offset_y in range(-radius, radius + 1):
            half_width = int(math.sqrt(
                max(0, radius * radius - offset_y * offset_y)))
            commands.append(self.fill(
                int(center_x) - half_width, int(center_y) + offset_y,
                half_width * 2 + 1, 1, color))
        return commands

    def _hint_box_geometry(self, message, center_x, max_width, min_width,
                           font):
        label = str(message).upper()
        font = self.normalize_font_for_text(font, label)
        available = max(1, int(max_width) - 2 * self.HINT_TEXT_PADDING)
        width = min(
            int(max_width),
            max(int(min_width),
                self.text_width(label, font) + 2 * self.HINT_TEXT_PADDING))
        x = int(center_x) - width // 2
        return label, font, available, x, width

    def hint_box(self, message, center_x, y, max_width=740, min_width=180,
                 height=44, border=ThemeColor.SECONDARY,
                 background=ThemeColor.BACKGROUND, font="JetBrainsMono 8pt"):
        """Draw a centered one-line hint with guaranteed inner padding."""
        label, font, available, x, width = self._hint_box_geometry(
            message, center_x, max_width, min_width, font)
        commands = self.panel(
            x, int(y), width, int(height), border=border,
            background=background, line_width=2)
        commands.append(self.text(
            int(center_x), int(y) + int(height) // 2, label, ThemeColor.TEXT,
            font, "center", "middle", max_width=available, truncate=True))
        return commands

    def section_panel(self, title, x, y, width, height, border=ThemeColor.BORDER):
        """Draw a titled content panel with consistent Feather spacing."""
        commands = self.panel(
            x, y, width, height, border=border, background=ThemeColor.PANEL,
            line_width=1)
        commands.append(self.text(
            x + 18, y + 21, str(title).upper(), ThemeColor.PRIMARY,
            "JetBrainsMono 8pt", "left", "middle"))
        return commands

    def numeric_keypad(self, x, y, width, height, title, value, actions,
                       subtitle="", mode="decimal", minimum=None,
                       maximum=None, max_length=10, fraction_digits=None,
                       confirm_label="CONFIRM", border=ThemeColor.BORDER,
                       background=ThemeColor.PANEL,
                       title_color=ThemeColor.TEXT,
                       subtitle_color=ThemeColor.DIM,
                       input_border=ThemeColor.SECONDARY,
                       value_color=ThemeColor.BRIGHT):
        """Draw a self-contained numeric entry window inside page chrome."""
        spec = (mode if isinstance(mode, NumericInputSpec) else
                NumericInputSpec(mode, minimum, maximum, max_length,
                                 fraction_digits))
        actions = dict(actions or {})
        x, y, width, height = map(int, (x, y, width, height))
        padding = max(5, min(10, width // 80, height // 45))
        gap = max(6, min(10, width // 90))
        inner_x = x + padding
        inner_width = width - padding * 2
        title_height = 44
        input_height = 50
        input_gap = 10
        confirm_height = 54
        confirm_gap = 10
        row_gap = 7
        keys_top = y + padding + title_height + input_height + input_gap
        keys_height = (height - padding * 2 - title_height - input_height
                       - input_gap - confirm_gap - confirm_height)
        key_height = max(1, (keys_height - row_gap * 3) // 4)
        confirm_y = y + height - padding - confirm_height
        key_width = max(1, (inner_width - gap * 2) // 3)
        commands = self.panel(
            x, y, width, height, border=border, background=background,
            line_width=1)
        commands.append(self.text(
            x + width // 2,
            y + padding + (12 if subtitle else title_height // 2),
            str(title).upper(), title_color, "JetBrainsMono Bold 8pt",
            "center", "middle",
            max_width=inner_width - 24, truncate=True))
        if subtitle:
            commands.append(self.text(
                x + width // 2, y + padding + 31,
                str(subtitle).upper(), subtitle_color,
                "JetBrainsMono 8pt", "center", "middle",
                max_width=inner_width - 24, truncate=True))
        input_y = y + padding + title_height
        commands += self.panel(
            inner_x, input_y, inner_width, input_height,
            border=input_border, background=background, line_width=2)
        sign_action = (actions.get("sign")
                       if spec.allows_negative and spec.allows_decimal
                       else None)
        sign_width = 68 if sign_action is not None else 0
        commands.append(self.text(
            inner_x + 16, input_y + input_height // 2, value or "_",
            value_color,
            "JetBrainsMono 12pt", "left", "middle",
            max_width=max(1, inner_width - 32 - sign_width), truncate=True))
        if sign_action is not None:
            commands += self.button(
                sign_action, inner_x + inner_width - sign_width, input_y,
                sign_width, input_height, "+/-", state="keypad_aux",
                font="JetBrainsMono 8pt")
        auxiliary = "decimal" if spec.allows_decimal else (
            "sign" if spec.allows_negative else None)
        rows = (("1", "2", "3"), ("4", "5", "6"),
                ("7", "8", "9"), (auxiliary, "0", "backspace"))
        for row, keys in enumerate(rows):
            key_y = keys_top + row * (key_height + row_gap)
            for column, token in enumerate(keys):
                key_x = inner_x + column * (key_width + gap)
                label = ({"decimal": ".", "sign": "+/-",
                          "backspace": "BACK"}.get(token, token) or "-")
                action = actions.get(token) if token is not None else None
                if action is None:
                    state = "disabled"
                elif token in ("decimal", "sign", "backspace"):
                    state = "keypad_aux"
                else:
                    state = "keypad"
                commands += self.button(
                    action or "numeric.disabled.%d.%d" % (row, column),
                    key_x, key_y, key_width, key_height, label,
                    state=state,
                    font=("JetBrainsMono 8pt" if token == "backspace"
                          else "JetBrainsMono 12pt"))
        confirm_action = actions.get("confirm")
        commands += self.button(
            confirm_action or "numeric.disabled.confirm", inner_x, confirm_y,
            inner_width, confirm_height, confirm_label,
            state=("keypad_confirm" if confirm_action is not None
                   and spec.is_valid(value) else "disabled"),
            font="JetBrainsMono Bold 10pt")
        return commands

    def dot_grid(self, x, y, width, height, columns=11, rows=7,
                 color=ThemeColor.MUTED, clip=None):
        """Draw a sparse point grid, optionally limited to a redraw region."""
        commands = []
        if columns < 2 or rows < 2:
            return commands
        if clip is not None:
            clip_x, clip_y, clip_width, clip_height = clip
            clip_right = clip_x + clip_width
            clip_bottom = clip_y + clip_height
        for row in range(rows):
            point_y = y + row * height // (rows - 1)
            for column in range(columns):
                point_x = x + column * width // (columns - 1)
                if (clip is None
                        or (clip_x <= point_x < clip_right
                            and clip_y <= point_y < clip_bottom)):
                    commands.append(self.fill(point_x, point_y, 1, 1, color))
        return commands

    def corner_marks(self, x, y, width, height, length=12, color=ThemeColor.PRIMARY):
        """Draw four open targeting corners around a control surface."""
        right = x + width - 1
        bottom = y + height - 1
        commands = []
        for horizontal_x, vertical_x in ((x, x), (right - length + 1, right)):
            commands.append(self.fill(horizontal_x, y, length, 1, color))
            commands.append(self.fill(vertical_x, y, 1, length, color))
            commands.append(self.fill(horizontal_x, bottom, length, 1, color))
            commands.append(self.fill(
                vertical_x, bottom - length + 1, 1, length, color))
        return commands

    def metric_row(self, x, y, width, label, value, unit="",
                   label_color=ThemeColor.PRIMARY, value_color=ThemeColor.TEXT,
                   label_font="JetBrainsMono 8pt", value_font="JetBrainsMono 8pt"):
        """Draw a compact label/value/unit row for status cards.

        When the label and the value do not fit side by side, each gets a
        share of the row and Typer truncates the text that exceeds it with an
        ellipsis: the longer text is shortened first, both when neither fits
        in half the row.
        """
        value_right = width - (34 if unit else 0)
        label, value = str(label), str(value)
        metrics = get_font_metrics()
        label_width = metrics.text_width(label, label_font)
        value_width = metrics.text_width(value, value_font)
        available = max(2, value_right - 4)  # 4 px apart
        label_limit = value_limit = None
        if label_width + value_width > available:
            half = available // 2
            if label_width <= half:
                value_limit = available - label_width
            elif value_width <= half:
                label_limit = available - value_width
            else:
                label_limit, value_limit = half, available - half

        def row_text(text_x, text, color, font, align, limit):
            if limit is None:
                return self.text(text_x, y, text, color, font, align, "middle")
            return self.text(text_x, y, text, color, font, align, "middle",
                             max_width=limit, truncate=True)

        commands = [
            row_text(x, label, label_color, label_font, "left", label_limit),
            row_text(x + value_right, value, value_color, value_font, "right", value_limit),
        ]
        if unit:
            commands.append(self.text(
                x + width, y, unit, label_color, label_font,
                "right", "middle"))
        return commands

    def vertical_gauge(self, x, y, width, height, title, value,
                       minimum, maximum, initial=None,
                       value_color=ThemeColor.PRIMARY):
        """Draw an auto-scaled vertical gauge with a movable start marker."""
        value = float(value)
        minimum = float(minimum)
        maximum = float(maximum)
        if maximum <= minimum:
            padding = max(1.0, abs(value) * 0.05)
            minimum = value - padding
            maximum = value + padding
        track_top = y + 78
        track_bottom = y + height - 28
        track_height = max(1, track_bottom - track_top)
        track_x = x + (width - 16) // 2
        track_width = 16

        def gauge_y(sample):
            ratio = ((float(sample) - minimum) / (maximum - minimum))
            ratio = max(0.0, min(1.0, ratio))
            return track_bottom - int(round(ratio * track_height))

        def number(sample):
            sample = float(sample)
            if abs(sample) < 10000:
                return str(round(sample))
            return "%.0e" % sample

        value_y = gauge_y(value)
        commands = self.panel(
            x, y, width, height, border=ThemeColor.BORDER,
            background=ThemeColor.PANEL, line_width=1)
        commands += [
            self.text(x + width // 2, y + 20, str(title).upper(),
                      ThemeColor.PRIMARY, "JetBrainsMono 8pt", "center", "middle"),
            self.text(x + width // 2, y + 48, number(value),
                      ThemeColor.TEXT, "JetBrainsMono Bold 8pt",
                      "center", "middle"),
        ]
        commands += [
            self.fill(track_x, track_top, track_width, track_height,
                      ThemeColor.MUTED),
            self.stroke(track_x, track_top, track_width, track_height,
                        ThemeColor.BORDER, 1),
            self.fill(track_x + 2, value_y,
                      max(1, track_width - 4),
                      max(1, track_bottom - value_y), value_color),
        ]
        if initial is not None:
            marker_y = gauge_y(initial)
            commands += [
                self.fill(x + 6, marker_y, max(1, width - 12), 2,
                          ThemeColor.SECONDARY),
                self.fill(x + 3, marker_y - 2, 4, 6, ThemeColor.SECONDARY),
            ]
        return commands

    def joystick_knob(self, x, y, axis="xy", size=25, color=ThemeColor.PRIMARY, *,
                      glyph=None, background=ThemeColor.PANEL):
        """Draw a centered knob with a glyph independent of its movement axis."""
        if glyph is None:
            glyph = "bars" if axis == "z" else "cross"
        if glyph not in ("bars", "cross", "none"):
            raise ValueError("Unknown knob glyph: %s" % glyph)
        size = max(9, int(size))
        if size % 2 == 0:
            size += 1
        left = int(x) - size // 2
        top = int(y) - size // 2
        commands = self.panel(
            left, top, size, size, border=color, background=background,
            line_width=2)
        center_x = int(x)
        center_y = int(y)
        if glyph == "bars":
            commands += [
                self.fill(center_x - 6, center_y - 3, 13, 1, color),
                self.fill(center_x - 6, center_y + 3, 13, 1, color),
            ]
        elif glyph == "cross":
            commands += [
                self.fill(center_x - 6, center_y, 13, 1, color),
                self.fill(center_x, center_y - 6, 1, 13, color),
            ]
        return commands

    # normalize_font(font, allow_proportional=False) maps UI font requests to sizes compiled into typer;
    # normalize_font_for_text(font, value) uses the requested face when it covers the rendered text. Both
    # measure on the engine catalog of _font_metrics().
    normalize_font = classmethod(renderer_normalize_font)
    normalize_font_for_text = classmethod(renderer_normalize_font_for_text)

    @classmethod
    def font_advance(cls, font):
        metric = get_font_metrics().metric(font)
        if not metric.monospaced:
            raise ValueError("font is not monospaced: %s" % metric.name)
        return metric.advance_x

    # text_width(value, font): the advance typer draws value with.
    text_width = classmethod(renderer_text_width)

    @property
    def hitboxes(self):
        """Ordered prepared input regions, including repeated actions."""
        return tuple(Hitbox(action, *spec, layer)
                     for layer, entries in (("base", self._hitboxes),
                                            ("overlay", self._overlay_hitboxes))
                     for action, spec in entries)

    def overlay_hitbox(self, action, x, y, width, height, continuous=False):
        if not self._prepare_surface():
            return ""
        logical = action_wire_id(action) if isinstance(action, Action) else str(action)
        self._register_region(logical, x, y, width, height, continuous, overlay=True)
        return self._hitbox(
            self._wire_action(action), x, y, width, height,
            continuous, layer="overlay")

    def _toggle_commands(self, x, y, width, height, thumb_x, enabled):
        border = ThemeColor.PRIMARY if enabled else ThemeColor.MUTED
        thumb_color = ThemeColor.PRIMARY if enabled else ThemeColor.DIM
        inset = 5
        thumb_size = max(1, height - 2 * inset)
        return [
            self.fill(x, y, width, height, ThemeColor.PANEL),
            self.stroke(x, y, width, height, border, 2),
            self.fill(thumb_x, y + inset, thumb_size, thumb_size, thumb_color),
        ]

    def toggle(self, action, x, y, width, height, active, enabled=True):
        """Draw a rectangular switch with a centered square thumb and no text."""
        if not self._prepare_surface():
            return []
        logical_action = action_wire_id(action) if isinstance(action, Action) else str(action)
        inset = 5
        thumb_size = max(1, height - 2 * inset)
        half = width // 2
        left = x + (half - thumb_size) // 2
        right = x + half + (half - thumb_size) // 2
        thumb_x = right if active else left
        self._toggles[logical_action] = (
            x, y, width, height, bool(active), enabled, left, right)
        commands = self._toggle_commands(
            x, y, width, height, thumb_x, enabled)
        if enabled:
            commands.append(self.action_hitbox(action, x, y, width, height))
        else:
            self._unregister_region(logical_action, x, y, width, height)
        return commands

    def animate_toggle(self, action, active, scheduler, duration=0.12):
        if not self.render_allowed:
            return False
        spec = self._toggles.get(action)
        if spec is None or not spec[5]:
            return False
        x, y, width, height, current, enabled, left, right = spec
        if current == bool(active):
            return False
        start, finish = (left, right) if active else (right, left)
        generation = self._generation
        frames = 4
        self._toggles[action] = (x, y, width, height, bool(active), enabled,
                                 left, right)

        def draw_frame(_eventtime, index):
            if self._generation != generation:
                return
            thumb_x = start + (finish - start) * index // frames
            self.prioritize_next_batch("animation", "toggle:%s" % action)
            self.send(self._toggle_commands(
                x, y, width, height, thumb_x, enabled))

        draw_frame(None, 1)
        for index in range(2, frames + 1):
            scheduler(lambda eventtime, step=index:
                      draw_frame(eventtime, step),
                      duration * (index - 1) / (frames - 1))
        return True

    def block_input(self):
        if not self._prepare_surface():
            return None
        self._reset_interactions()
        self.send([
            self.clear_hitboxes("base"),
            self.clear_hitboxes("overlay"),
            self._wake_hitbox(),
        ])

    def _reset_interactions(self):
        self._buttons = {}
        self._toggles = {}
        self._hitboxes = []
        self._overlay_hitboxes = []
        self._pressed_buttons = set()

    def button_surface(self, action, x, y, width, height,
                       normal_commands, pressed_commands):
        """Register complete normal and pressed surfaces for a rich button."""
        if not self._prepare_surface():
            return []
        logical_action = (
            action_wire_id(action) if isinstance(action, Action) else str(action))
        normal = tuple(normal_commands)
        pressed = tuple(pressed_commands)
        self._set_button(logical_action, _ButtonSpec(
            x, y, width, height, "", "enabled", None,
            None, "surface", None, None, None, (normal, pressed)))
        surface = pressed if logical_action in self._pressed_buttons else normal
        return list(surface) + [
            self.action_hitbox(action, x, y, width, height)]

    def _begin_dialog(self, bounds, modal):
        self._dialog_bounds = bounds if modal else self._dialog_bounds + bounds
        if not modal:
            return []
        self._reset_interactions()
        return [self.clear_hitboxes("base"), self.clear_hitboxes("overlay"), self._wake_hitbox()]

    def popup(self, dismiss, x, y, width, height, *, border=ThemeColor.PRIMARY,
              background=ThemeColor.PANEL, line_width=2, preserve_header_action=True):
        """Prepare one modal panel; its component root supplies arbitrary content."""
        if not self._prepare_surface():
            return []
        commands = self._begin_dialog(((x, y, width, height),), True)
        commands.append(self.action_hitbox(dismiss, 0, 0, SCREEN_WIDTH, SCREEN_HEIGHT))
        commands += self.panel(x, y, width, height, border, background, line_width)
        if preserve_header_action and self._header_action is not None:
            commands += self._header_action_commands()
            action = self._header_action[0]
            wire = action_wire_id(action) if isinstance(action, Action) else str(action)
            for region in self.hitboxes:
                if region.action == wire:
                    commands.append(self.overlay_hitbox(wire, region.x, region.y, region.width, region.height))
                    break
        return commands

    def dialog(self, title, lines, buttons, x=160, y=130, width=480,
               height=220, tone="warning", modal=True,
               preserve_header_action=True, page=0, page_actions=None,
               custom_body=False, button_groups=()):
        """Build a modal dialog from standard panel, text, and button primitives.

        ``buttons`` contains persistent footer ``(action, label, state)`` tuples.
        ``button_groups`` contains body rows of the same tuples. Clearing all
        existing hitboxes makes a dialog genuinely modal even when it only
        covers one control region visually. Set ``modal`` to false for a
        localized overlay whose caller will explicitly re-register the
        controls that remain available. Set ``preserve_header_action`` to
        false when loss of input makes even that action unusable.
        Set ``custom_body`` when the caller draws its own content in the panel.
        """
        if not self._prepare_surface():
            return []
        tones = {
            "warning": ThemeColor.WARNING,
            "danger": ThemeColor.DANGER,
            "info": ThemeColor.PRIMARY,
        }
        border = tones.get(tone, ThemeColor.PRIMARY)
        button_groups = tuple(tuple(group) for group in button_groups if group)
        x, y, width, height, title_rows, visible_lines, page, page_count, has_footer, footer_height, rows = (
            dialog_layout(
                self, _DIALOG_POLICY + (self.DIALOG_TEXT_PADDING, self.BUTTON_TEXT_PADDING, SCREEN_WIDTH,
                                        SCREEN_HEIGHT),
                title, tuple(lines), tuple(buttons), button_groups, x, y, width, height, modal and not custom_body,
                custom_body, page))
        if page_count > 1 and (page_actions is None or len(page_actions) != 2):
            raise ValueError("Overflowing dialog requires previous and next actions")
        outer_x = max(0, x - 8)
        outer_y = max(HEADER_BOTTOM + 1, y - 8)
        outer_right = min(SCREEN_WIDTH, x + width + 8)
        outer_bottom = min(CONTENT_BOTTOM, y + height + 8)
        outer_bounds = (outer_x, outer_y, outer_right - outer_x,
                        outer_bottom - outer_y)
        commands = self._begin_dialog(((x, y, width, height),), modal)
        show_header_action = (preserve_header_action and self._header_action is not None)
        if modal:
            # Hide fragments of retained controls immediately beside the frame.
            commands.append(self.fill(*outer_bounds, ThemeColor.BACKGROUND))
        commands += self.panel(
            x, y, width, height, border=border, background=ThemeColor.PANEL)
        for line, title_y in title_rows:
            commands.append(self.text(
                x + width // 2, title_y, line, border,
                DIALOG_TITLE_FONT, "center", "middle",
                max_width=width - 2 * self.DIALOG_TEXT_PADDING,
                truncate=True))
        pager_reserve = DIALOG_PAGER_RESERVE if page_count > 1 else 0
        for index, line in enumerate(visible_lines):
            commands.append(self.text(
                x + (width - pager_reserve) // 2,
                y + DIALOG_BODY_Y + index * DIALOG_LINE_SPACING,
                str(line), ThemeColor.TEXT, DIALOG_BODY_FONT, "center", "middle",
                max_width=width - 2 * self.DIALOG_TEXT_PADDING - pager_reserve,
                truncate=True))
        if page_count > 1:
            commands += self.dialog_pager(
                page, page_count, page_actions, x, y, width, height,
                has_buttons=has_footer, footer_height=footer_height)
        for specs, font, bounds in rows:
            for (action, label, state), target in zip(specs, bounds):
                commands += self.button(
                    action, *target, label, state=state, font=font)
        if modal and show_header_action:
            commands += self._header_action_commands()
        return commands

    def status_dialog(self, title, detail=None, buttons=(), *, description=None,
                      note=None, note_color=ThemeColor.TEXT, tone="warning",
                      width=DIALOG_MIN_WIDTH, height=DIALOG_MIN_HEIGHT,
                      modal=True, preserve_header_action=True):
        """Render optional value, wrapped explanation, and note below a status.

        Empty slots take no space. The explanation may be truncated to keep
        the note and standard footer actions visible. Use ``dialog`` for
        instructions that need pagination or more than two rows of actions.
        """
        buttons = tuple(buttons)
        screen_width = getattr(self, "screen_width", SCREEN_WIDTH)
        screen_height = getattr(self, "screen_height", SCREEN_HEIGHT)
        # The dialog layout fits the panel to the title and actions and wraps
        # the actions into footer rows; more than two leave the footer.
        x, _y, width, _height, _titles, _lines, _page, _count, has_footer, footer_height, _rows = dialog_layout(
            self, _DIALOG_POLICY + (self.DIALOG_TEXT_PADDING, self.BUTTON_TEXT_PADDING, screen_width,
                                    screen_height),
            title, (), buttons, (), 0, 0, int(width), int(height), True, True, 0)
        if buttons and not has_footer:
            raise ValueError("Status dialog needs a paginated dialog for more than two action rows")

        text_width = width - 2 * self.DIALOG_TEXT_PADDING
        metrics = get_font_metrics()
        slots = []
        for value, font, color, wrap in (
                (detail, DIALOG_STATUS_FONT, ThemeColor.TEXT, False),
                (description, DIALOG_BODY_FONT, ThemeColor.TEXT, True),
                (note, DIALOG_STATUS_FONT, note_color, False)):
            if value is None or not str(value).strip():
                continue
            value = str(value)
            font = self.normalize_font_for_text(font, value)
            text_height = metrics.text_height(value, font, max_width=text_width, wrap=wrap)
            slots.append((value, font, color, wrap, text_height))

        title_bottom = DIALOG_TITLE_TOP + metrics.metric(DIALOG_TITLE_FONT).glyph_height
        bottom_space = DIALOG_BUTTON_BOTTOM + footer_height
        if has_footer:
            bottom_space += DIALOG_STATUS_GAP
        body_limit = (screen_height - DIALOG_TOP - DIALOG_BOTTOM_INSET
                      - title_bottom - DIALOG_STATUS_GAP - bottom_space)
        body_height = sum(slot[4] for slot in slots) + DIALOG_STATUS_GAP * max(0, len(slots) - 1)
        for index, (value, font, color, wrap, text_height) in enumerate(slots):
            if wrap and body_height > body_limit:
                available = text_height - (body_height - body_limit)
                if available >= metrics.metric(font).glyph_height:
                    slots[index] = (value, font, color, wrap, available)
                    body_height = body_limit
        if body_height > body_limit:
            raise ValueError("Status dialog content does not fit; use a paginated dialog")

        height = max(DIALOG_MIN_HEIGHT, int(height))
        y, height = dialog_vertical_bounds(
            (screen_height - height) // 2, height, 0, has_footer,
            screen_height, minimum_height=title_bottom + DIALOG_STATUS_GAP + body_height + bottom_space,
            footer_height=footer_height)
        commands = self.dialog(
            title, (), buttons, x=x, y=y, width=width, height=height,
            tone=tone, modal=modal, custom_body=True,
            preserve_header_action=preserve_header_action)
        if not commands:
            return commands

        body_top = y + title_bottom + DIALOG_STATUS_GAP
        body_bottom = y + height - bottom_space
        cursor = body_top + (body_bottom - body_top - body_height) // 2
        for value, font, color, wrap, text_height in slots:
            commands.append(self.text(
                x + width // 2, cursor + text_height // 2, value, color, font, "center", "middle",
                max_width=text_width, max_height=text_height, wrap=wrap, truncate=True))
            cursor += text_height + DIALOG_STATUS_GAP
        return commands

    def dialog_pager(self, page, page_count, actions, x, y, width, height,
                     *, has_buttons=True, footer_height=DIALOG_BUTTON_HEIGHT):
        """Draw the narrow vertical page controls shared by dialog panels."""
        if page_count <= 1:
            return []
        previous, following = actions
        prev_bounds, next_bounds, counter = dialog_pager_bounds(
            x, y, width, height, has_buttons, footer_height)
        commands = self.arrow_button(
            previous, *prev_bounds, "up", active=page > 0)
        commands += self.arrow_button(
            following, *next_bounds, "down", active=page + 1 < page_count)
        if has_buttons and footer_height > DIALOG_BUTTON_HEIGHT:
            commands.append(self.text(
                counter[0], (prev_bounds[1] + prev_bounds[3] + next_bounds[1]) // 2,
                "%d/%d" % (page + 1, page_count), ThemeColor.DIM,
                DIALOG_BODY_FONT, "center", "middle",
                max_width=DIALOG_PAGER_RESERVE - 8, truncate=True))
            return commands
        for offset, value in ((-24, page + 1), (24, page_count)):
            commands.append(self.text(
                counter[0], counter[1] + offset, str(value), ThemeColor.DIM,
                DIALOG_BODY_FONT, "center", "middle",
                max_width=DIALOG_PAGER_RESERVE - 8, truncate=True))
        commands.append(self.fill(
            counter[0] - 11, counter[1] - 1, 22, 2, ThemeColor.DIM))
        return commands

    def flash_button(self, action):
        spec = self._visible_input("_buttons").get(action)
        if spec is None:
            return False
        self._press(action, visible=True)
        self.prioritize_next_batch("animation", "button:%s" % action)
        if spec.layout == "surface":
            self.send(spec.surfaces[1], button_feedback=True)
        elif spec.layout in ("arrow-up", "arrow-down"):
            self.send(self._arrow_button_commands(
                action, spec.x, spec.y, spec.width, spec.height,
                spec.layout[6:], "pressed", False), button_feedback=True)
        else:
            self.send(self._button_commands(
                action, spec.x, spec.y, spec.width, spec.height,
                spec.label, "pressed", spec.font, spec.subtitle, False,
                spec.layout, spec.subtitle_font, spec.subtitle_color,
                spec.accent), button_feedback=True)
        return True

    def restore_button(self, action):
        spec = self._visible_input("_buttons").get(action)
        self._press(action, False, visible=True)
        if spec is None:
            return False
        self.prioritize_next_batch("state", "button:%s" % action)
        if spec.layout == "surface":
            self.send(spec.surfaces[0], button_feedback=True)
        elif spec.layout in ("arrow-up", "arrow-down"):
            self.send(self._arrow_button_commands(
                action, spec.x, spec.y, spec.width, spec.height,
                spec.layout[6:], spec.state, False), button_feedback=True)
        else:
            self.send(self._button_commands(
                action, spec.x, spec.y, spec.width, spec.height,
                spec.label, spec.state, spec.font, spec.subtitle, False,
                spec.layout, spec.subtitle_font, spec.subtitle_color,
                spec.accent), button_feedback=True)
        return True

    def set_header_action(self, action=None, label="", state="danger",
                          font="JetBrainsMono Bold 8pt"):
        if not self._prepare_surface():
            return False
        value = (None if action is None else
                 (action, str(label), str(state), str(font)))
        if value == self._header_action:
            return False
        previous = self._header_action
        self._header_action = value
        if previous is not None:
            old_action = previous[0]
            old_wire_id = (action_wire_id(old_action)
                           if isinstance(old_action, Action)
                           else str(old_action))
            self._pop_button(old_wire_id)
            self._press(old_wire_id, False)
        return True

    def _header_action_commands(self):
        if self._header_action is None:
            return []
        action, label, state, font = self._header_action
        return self.button(
            action, *self._header_action_bounds, label, state=state, font=font)

    def redraw_page(self):
        """Rebuild interactions and pixels through the normal page lifecycle."""
        return self.begin_page(self._page_title, back=self._page_back)

    @contextmanager
    def rebuild_page_input(self):
        """Replace page input and feedback without repainting unchanged pixels."""
        self.invalidate_input_generation()
        self.redraw_page_hitboxes()
        commands = []
        yield commands
        active = self._keep_input()
        self._toggles = {action: spec for action, spec in self._toggles.items() if action in active}
        # Paint commands may contain old IDs. Final replay replaces all of
        # them, including retained overlay regions, with this generation.
        commands.extend(self._input_commands())
        self.prioritize_next_batch("state")

    def begin_page(self, title, back=False):
        if not self._prepare_surface():
            return []
        self._page_title = title
        self._page_back = back
        self._loader_active = False
        self._semantic_page_id = None
        self.invalidate_input_generation()
        self._reset_interactions()
        self._menu_suppressed = False
        show_header_action = self._header_action is not None
        commands = [
            self.clear_hitboxes("base"),
            self.clear_hitboxes("overlay"),
            self._wake_hitbox(),
            # Preserve the footer framebuffer. It is a persistent status area
            # and is updated independently only when one of its values changes.
            self.fill(0, 0, SCREEN_WIDTH, FOOTER_Y - 2, ThemeColor.BACKGROUND),
            # Keep the dirty rectangle above the persistent footer. A full
            # height outer stroke would make TextDrawer.flush() copy the whole
            # footer even though none of its pixels changed.
            self.stroke(FRAME_X, FRAME_Y, FRAME_WIDTH,
                        FOOTER_Y - FRAME_Y - 1,
                        ThemeRole.HEADER_BORDER, 1),
            self.fill(10, 6, 780, HEADER_BOTTOM - 6, ThemeRole.HEADER_BACKGROUND),
        ]
        if back:
            commands += self.button("nav.back", *self._back_bounds, "< BACK",
                                    font="JetBrainsMono Bold 8pt")
        title = str(title).upper()
        title_width = 440 if back or show_header_action or self._busy_label is not None else 740
        commands += [
            self.text(400, 29, title, ThemeRole.HEADER_TEXT,
                      "JetBrainsMono 12pt",
                      "center", "middle", max_width=title_width, truncate=True),
            self.fill(18, HEADER_BOTTOM, 764, 1, ThemeRole.HEADER_BORDER),
            self.fill(18, FOOTER_Y - 2, 764, 1, ThemeColor.BORDER),
        ]
        if self._busy_label is not None and not show_header_action:
            busy_label = self._busy_label
            commands += [
                self.fill(BUSY_NOTICE_X, BUSY_NOTICE_Y,
                          BUSY_NOTICE_WIDTH, BUSY_NOTICE_HEIGHT,
                          ThemeRole.HEADER_BACKGROUND),
                self.stroke(BUSY_NOTICE_X, BUSY_NOTICE_Y,
                            BUSY_NOTICE_WIDTH, BUSY_NOTICE_HEIGHT,
                            ThemeColor.WARNING, 2),
                self.text(BUSY_NOTICE_X + BUSY_NOTICE_WIDTH // 2,
                          BUSY_NOTICE_Y + BUSY_NOTICE_HEIGHT // 2,
                          busy_label, ThemeColor.WARNING,
                          "JetBrainsMono Bold 8pt", "center", "middle",
                          max_width=132, truncate=True),
            ]
        if show_header_action:
            commands += self._header_action_commands()
        if self._footer_values is not None and not self._footer_drawn:
            commands += self._paint_footer(self._footer_values)
        return commands

    def _paint_footer(self, values):
        commands = self._footer_commands(values)
        self._last_footer = values
        self._footer_drawn = True
        return commands

    def _footer_commands(self, values):
        left, right = values
        return [
            # The footer is a persistent framebuffer region. Clear its full
            # extent before drawing the inner panel so startup overlays and
            # previous themes cannot survive in the outer gutters or bottom
            # rows. Space outside the framed panel follows the page background.
            self.fill(
                0, FOOTER_Y - 2, SCREEN_WIDTH,
                SCREEN_HEIGHT - (FOOTER_Y - 2), ThemeColor.BACKGROUND),
            self.fill(10, FOOTER_Y, 780, FOOTER_HEIGHT - 1, ThemeColor.PANEL),
            self.stroke(FRAME_X, FOOTER_Y - 2, FRAME_WIDTH,
                        SCREEN_HEIGHT - (FOOTER_Y - 2) - 4, ThemeColor.BORDER, 1),
            self.text(20, FOOTER_Y + 16, left, ThemeColor.PRIMARY,
                      "JetBrainsMono 8pt", max_width=380, truncate=True),
            self.text(780, FOOTER_Y + 16, right, ThemeColor.PRIMARY,
                      "JetBrainsMono 8pt", "right", max_width=340,
                      truncate=True),
        ]

    def footer(self, left, right, paint=True):
        values = (str(left), str(right))
        self._footer_values = values
        if not paint:
            return
        if self._footer_drawn and values == self._last_footer:
            return
        if not self._prepare_surface():
            return
        self.prioritize_next_batch("state", "footer")
        self.send(self._paint_footer(values))

    def invalidate_footer(self):
        """Require the next page frame to restore the persistent footer."""
        self._footer_drawn = False

    def toast(self, message):
        height = 44
        y = CONTENT_BOTTOM - height - 8
        _label, _font, _available, x, width = self._hint_box_geometry(
            message, 400, 740, 180, "JetBrainsMono 8pt")
        commands = self.hint_box(
            message, 400, y, max_width=740, min_width=180, height=height,
            border=ThemeColor.SECONDARY, background=ThemeColor.BACKGROUND,
            font="JetBrainsMono 8pt")
        commands.append(self.overlay_hitbox(
            DismissToast(), x, y, width, height))
        self.prioritize_next_batch("state", "toast")
        self.send(commands)

    def clear_toast_hitbox(self):
        if not self._prepare_surface():
            return
        self._overlay_hitboxes = []
        self.prioritize_next_batch("state", "toast-hitbox")
        self.send([self.clear_hitboxes("overlay")])

    def busy_notice(self, label="PLEASE WAIT"):
        label = str(label).upper()
        if label == self._busy_label:
            return
        self._busy_label = label
        if self._header_action is not None:
            return
        self.send([
            self.fill(BUSY_NOTICE_X, BUSY_NOTICE_Y,
                      BUSY_NOTICE_WIDTH, BUSY_NOTICE_HEIGHT,
                      ThemeRole.HEADER_BACKGROUND),
            self.stroke(BUSY_NOTICE_X, BUSY_NOTICE_Y,
                        BUSY_NOTICE_WIDTH, BUSY_NOTICE_HEIGHT,
                        ThemeColor.WARNING, 2),
            self.text(BUSY_NOTICE_X + BUSY_NOTICE_WIDTH // 2,
                      BUSY_NOTICE_Y + BUSY_NOTICE_HEIGHT // 2,
                      label, ThemeColor.WARNING,
                      "JetBrainsMono Bold 8pt", "center", "middle",
                      max_width=132, truncate=True),
        ])

    def clear_busy_notice(self):
        if self._busy_label is None:
            return
        self._busy_label = None
        if self._header_action is not None:
            return
        commands = [
            self.fill(BUSY_NOTICE_X, BUSY_NOTICE_Y,
                      BUSY_NOTICE_WIDTH, BUSY_NOTICE_HEIGHT,
                      ThemeRole.HEADER_BACKGROUND)]
        menu = self._buttons.get("nav.menu")
        if menu is not None:
            commands += self._button_commands(
                "nav.menu", menu.x, menu.y, menu.width, menu.height,
                menu.label, menu.state, menu.font, menu.subtitle,
                self._menu_suppressed, menu.layout, menu.subtitle_font,
                menu.subtitle_color, menu.accent)
            self._menu_suppressed = False
        self.send(commands)

    def loader(self, message, phase=0):
        """Replace the page with a non-interactive yielding-operation view."""
        if not self._prepare_surface():
            return None
        # A loader is a new interaction surface, not a partial repaint. Bump
        # the generation so already queued Back/page events cannot target the
        # page underneath it.
        first_frame = not self._loader_active
        if first_frame:
            self.invalidate_input_generation()
            self._loader_active = True
        preserve_header_action = self._header_action is not None
        self._reset_interactions()
        commands = [
            self.clear_hitboxes("base"),
            self.clear_hitboxes("overlay"),
            self._wake_hitbox(),
            # Clear the header too: leaving the old Back button painted made
            # it look usable even though its hitbox had been removed.
            self.fill(0, 0, SCREEN_WIDTH, CONTENT_BOTTOM, ThemeColor.BACKGROUND),
            self.stroke(FRAME_X, FRAME_Y, FRAME_WIDTH,
                        FOOTER_Y - FRAME_Y - 1, ThemeRole.HEADER_BORDER, 1),
            self.fill(10, 6, 780, HEADER_BOTTOM - 6, ThemeRole.HEADER_BACKGROUND),
            self.text(400, 29, "OPERATION IN PROGRESS", ThemeRole.HEADER_TEXT,
                      "JetBrainsMono 12pt", "center", "middle",
                      max_width=440, truncate=True),
            self.fill(18, HEADER_BOTTOM, 764, 1, ThemeRole.HEADER_BORDER),
            self.fill(18, FOOTER_Y - 2, 764, 1, ThemeColor.BORDER),
            # Bottom-aligned so a second line grows into the free band under
            # the header; below it there is no room. 93 px = two lines.
            self.text(400, 215, message, ThemeColor.TEXT, "JetBrainsMono Bold 16pt",
                      "center", "bottom", max_width=704, max_height=93,
                      wrap=True, truncate=True),
            self.text(400, 235, "PLEASE WAIT", ThemeColor.DIM, "JetBrainsMono 12pt",
                      "center", "middle"),
        ]
        for index in range(5):
            color = ThemeColor.PRIMARY if index == phase % 5 else ThemeColor.MUTED
            commands.append(self.fill(290 + index * 48, 280, 32, 12, color))
        if preserve_header_action:
            commands += self._header_action_commands()
        self.prioritize_next_batch(
            "surface" if first_frame else "animation", "loader")
        self.send(commands)

    def startup_modal(self, title, detail, phase=0, critical=False):
        """Draw a pre-ready loading modal and its pulse frame."""
        if not self._prepare_surface():
            return None
        # This full-screen modal owns the framebuffer until the host is ready.
        # Invalidate animations scheduled by the page underneath it so a late
        # toggle/button frame cannot be painted over the restart screen.
        self.invalidate_input_generation()
        self._loader_active = True
        self._reset_interactions()
        commands = [
            self.clear_hitboxes("base"),
            self.clear_hitboxes("overlay"),
            self._wake_hitbox(),
            self.fill(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT, ThemeColor.OVERLAY),
        ]
        commands += self.panel(
            150, 120, 500, 250, border=ThemeColor.PRIMARY,
            background=ThemeColor.PANEL, line_width=2)
        commands += [
            self.text(400, 170, str(title), ThemeColor.PRIMARY,
                      "JetBrainsMono Bold 16pt", "center", "middle"),
        ]
        commands += self.startup_pulse(phase)
        commands += [
            self.text(400, 300, "PLEASE WAIT", ThemeColor.TEXT,
                      "JetBrainsMono 12pt", "center", "middle"),
            self.text(400, 335, str(detail), ThemeColor.DIM,
                      "JetBrainsMono 8pt", "center", "middle"),
        ]
        self.prioritize_next_batch(
            "critical" if critical else "surface", "startup")
        self.send(commands)

    def startup_pulse(self, phase=0):
        """Redraw only the animated startup indicator's small dirty area."""
        pulse = (8, 12, 16, 12)[int(phase) % 4]
        commands = [self.fill(374, 206, 53, 53, ThemeColor.PANEL)]
        commands += self.filled_circle(400, 232, pulse, ThemeColor.SECONDARY)
        return commands

    def touch_unavailable_modal(self):
        """Replace an interactive surface until touch input reconnects."""
        if not self._prepare_surface():
            return None
        self.invalidate_input_generation()
        self._footer_drawn = False
        commands = self.dialog(
            "Touch input unavailable", (), (),
            x=110, y=110, width=580, height=270, tone="warning",
            preserve_header_action=False, custom_body=True)
        commands.append(self.text(
            400, 245,
            "THE TOUCH DEVICE IS NOT AVAILABLE. "
            "WAITING FOR AUTOMATIC RECONNECTION.",
            ThemeColor.TEXT, "JetBrainsMono 12pt", "center", "middle",
            max_width=524, max_height=108, wrap=True, truncate=True))
        self.prioritize_next_batch("critical", "touch-unavailable")
        self.send(commands)

    def applying_modal(self, message="APPLYING CHANGES"):
        """Dim the page and draw a non-interactive modal progress panel."""
        if not self._prepare_surface():
            return None
        self.invalidate_input_generation()
        self._loader_active = True
        self._reset_interactions()
        commands = [
            self.clear_hitboxes("base"),
            self.clear_hitboxes("overlay"),
            self._wake_hitbox(),
            self.fill(0, HEADER_BOTTOM + 1, SCREEN_WIDTH,
                      CONTENT_BOTTOM - HEADER_BOTTOM - 1, ThemeColor.OVERLAY),
        ]
        commands += self.panel(160, 145, 480, 180)
        commands += [
            self.text(400, 205, str(message).upper(), ThemeColor.TEXT,
                      "JetBrainsMono Bold 16pt", "center", "middle"),
            self.text(400, 260, "PLEASE WAIT", ThemeColor.DIM,
                      "JetBrainsMono 12pt", "center", "middle"),
            self.fill(310, 292, 180, 8, ThemeColor.PRIMARY),
        ]
        self.prioritize_next_batch("surface", "applying-modal")
        self.send(commands)


def rectangles_overlap(first, second):
    """Test helper used to keep page hitboxes away from the footer."""
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah
