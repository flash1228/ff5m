## Declarative active-print page for Feather.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from enum import Enum

from ui import ThemeColor
from ui.bindings import bind, derived
from ui.components import Button, Disc, Fill, Image, ProgressBar, Stroke, Text
from ui.layout import FLEX, Column, Equal, Flex, Grid, Overlay, PageTree, Rect, Spacer, StateCase

from ..keys import AppPage
from ..styles import UI_STYLES, UiStyle
from .actions import CANCEL, HOME, MORE, PAUSE, RESUME, Z_ADJUST
from .components import METRIC_PAIR
from .state import PrintingState


PAGE_BOUNDS = Rect(0, 0, 800, 442)
BUTTON_FONT = "JetBrainsMono Bold 8pt"
PREVIEW_IMAGE_PADDING = 20


class PrintingRef(Enum):
    ROOT = "printing.root"
    HEADER = "printing.header"
    HOME = "printing.home"
    BODY = "printing.body"
    DETAILS = "printing.details"
    FILENAME = "printing.filename"
    STATUS = "printing.status"
    PROGRESS = "printing.progress"
    PROGRESS_VALUE = "printing.progress.value"
    ELAPSED = "printing.elapsed"
    REMAINING = "printing.remaining"
    LAYER = "printing.layer"
    HEIGHT = "printing.height"
    PREVIEW = "printing.preview"
    PREVIEW_BOX = "printing.preview.box"
    BUTTONS = "printing.buttons"
    PAUSE = "printing.pause"
    MORE = "printing.more"
    Z_ADJUST = "printing.z_adjust"
    CANCEL = "printing.cancel"


def _button_state(ready, pending, action):
    if not ready:
        return "disabled"
    return "busy" if pending == action else "enabled"


def _pause_state(ready, pending):
    return _button_state(ready, pending, "print.pause")


def _resume_state(ready, pending):
    return _button_state(ready, pending, "print.resume")


def _enabled(value):
    return "enabled" if value else "disabled"


def _details(compact=False):
    progress_value = bind(PrintingState.PROGRESS)

    information = Column(
        Text(
            bind(PrintingState.FILENAME),
            color=ThemeColor.PRIMARY,
            font="JetBrainsMono Bold 12pt",
            horizontal="left",
            vertical="top",
            truncate=True,
        ).height("content").ref(PrintingRef.FILENAME),
        Text(
            bind(PrintingState.STATUS),
            horizontal="left",
            vertical="top",
            truncate=True,
            # A long operation path keeps its innermost step.
            tail_separator=" -> ",
        ).style(UiStyle.TEXT).height("content").ref(PrintingRef.STATUS),
        gap=8,
    ).padding(top=4).height("content")

    progress = Column(
        Grid(
            matrix=((
                Text("PROGRESS").style(UiStyle.PRIMARY_LEFT),
                Text(
                    derived(lambda value: "%d%%" % value, progress_value),
                    color=ThemeColor.PRIMARY,
                    horizontal="right",
                ).style(UiStyle.VALUE).ref(PrintingRef.PROGRESS_VALUE),
            ),),
            columns=Equal(2), rows=Equal(1), gap=0,
        ).height("content"),
        ProgressBar(progress_value).height(34).ref(PrintingRef.PROGRESS),
        gap=4,
    ).height("content")

    timing = METRIC_PAIR(
        instance_key=PrintingRef.ELAPSED,
        left_label="ELAPSED", left_value=bind(PrintingState.ELAPSED), left_ref=PrintingRef.ELAPSED,
        right_label="REMAINING", right_value=bind(PrintingState.REMAINING), right_ref=PrintingRef.REMAINING,
    )
    position = METRIC_PAIR(
        instance_key=PrintingRef.LAYER,
        left_label="LAYER", left_value=bind(PrintingState.LAYER), left_ref=PrintingRef.LAYER,
        right_label="HEIGHT", right_value=bind(PrintingState.HEIGHT), right_ref=PrintingRef.HEIGHT,
    )

    # The details' own background lets a changed value repaint alone.
    return Overlay(Fill(ThemeColor.BACKGROUND), Column(
        information,
        progress,
        Spacer(),
        Fill(ThemeColor.BORDER).height(1),
        timing,
        Fill(ThemeColor.BORDER).height(1),
        position,
        gap=0 if compact else 2,
    )).ref(PrintingRef.DETAILS)


def _preview():
    status = bind(PrintingState.PREVIEW_STATUS)
    no_preview = Overlay(
        # Its own background lets the loader turn without repainting the panel.
        Fill(ThemeColor.PANEL),
        StateCase(Text("NO PREVIEW").style(UiStyle.DIM), selector=status, expected="none"),
        StateCase(Text("NO PREVIEW").style(UiStyle.DIM), selector=status, expected="failed"),
        StateCase(Image(bind(PrintingState.PREVIEW_BANDS)), selector=status, expected="ready"),
        # Last, with its own bounds, so a turn of the loader repaints the disc alone.
        StateCase(Disc(bind(PrintingState.LOADER_RADIUS)), selector=status, expected="loading")
        .size(72, 72).align("center", "center"),
    ).margin(left=8, right=8, bottom=8).ref(PrintingRef.PREVIEW_BOX)

    # The panel draws as a Frame with UiStyle.FRAME_PANEL; as an Overlay of its own pieces a change
    # inside it repaints only what changed (a Frame repaints whole).
    return Overlay(
        Fill(ThemeColor.PANEL),
        Stroke(ThemeColor.BORDER, 1),
        Column(
            Column(
                Text("PREVIEW").style(UiStyle.PRIMARY_LEFT).height("content"),
            ).height("content").padding(left=12, top=10, right=0, bottom=8),
            no_preview,
            gap=0,
        ),
    ).ref(PrintingRef.PREVIEW)


def _buttons():
    ready = bind(PrintingState.CONTROLS_READY)
    pending = bind(PrintingState.PENDING_ACTION)
    pause = Overlay(
        Fill(ThemeColor.BACKGROUND),
        StateCase(
            Button(PAUSE, "PAUSE", state=derived(_pause_state, ready, pending)).style(UiStyle.BUTTON_BOLD_8),
            selector=bind(PrintingState.PAUSED), expected=False),
        StateCase(
            Button(RESUME, "RESUME", state=derived(_resume_state, ready, pending)).style(UiStyle.BUTTON_BOLD_8),
            selector=bind(PrintingState.PAUSED), expected=True),
    ).ref(PrintingRef.PAUSE)

    return Grid(
        matrix=((
            pause,
            Button(Z_ADJUST, "Z ADJUST", state=derived(_enabled, bind(PrintingState.LIVE_Z_ALLOWED)))
            .style(UiStyle.BUTTON_BOLD_8).ref(PrintingRef.Z_ADJUST),
            Button(MORE, "MORE", state=derived(_enabled, ready))
            .style(UiStyle.BUTTON_BOLD_8).ref(PrintingRef.MORE),
            Button(CANCEL, "CANCEL", state="danger")
            .style(UiStyle.BUTTON_BOLD_8).ref(PrintingRef.CANCEL),
        ),),
        columns=Equal(4), rows=Equal(1), gap=(12, 0),
    ).margin(left=20, right=20).ref(PrintingRef.BUTTONS)


def create_page(bounds=PAGE_BOUNDS):
    compact = bounds.height < PAGE_BOUNDS.height
    header = Grid(
        matrix=((
            Button(HOME, "HOME").style(UiStyle.BUTTON_BOLD_8).ref(PrintingRef.HOME),
            Spacer(),
        ),),
        columns=(146, FLEX), rows=Equal(1), gap=12,
    ).padding(left=14, top=6, right=14, bottom=2).height(56).ref(PrintingRef.HEADER)

    content = Column(
        Grid(
            matrix=((_details(compact=compact), _preview()),),
            columns=(Flex(2), FLEX), rows=Equal(1), gap=(24, 0),
        ).margin(left=20, top=0, right=20, bottom=0).ref(PrintingRef.BODY),
        _buttons().height(48 if compact else 68),
        gap=18,
    )
    root = Column(header, content, gap=14) \
        .padding(left=0, top=0, right=0, bottom=10) \
        .ref(PrintingRef.ROOT)

    page = PageTree(
        root, bounds, page_id=AppPage.PRINTING,
        styles=UI_STYLES,
        component_templates=(METRIC_PAIR,),
    )
    page.title = "Printing"
    return page


PAGE = create_page()
