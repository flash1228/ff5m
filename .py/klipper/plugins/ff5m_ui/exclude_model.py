## Declarative object-exclusion page and its item geometry.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from ui import ThemeColor
from ui.actions import Command, ItemCommand
from ui.bindings import bind, derived, item, state
from ui.components import Button, Frame, Hitbox, PlacedFrame, Text
from ui.identity import CommandKey, StateKey
from ui.layout import Column, Equal, FLEX, Grid, ListView, Overlay, PageTree, Rect, Spacer, StateCase, Template

from .keys import AppPage
from .printing.actions import PrintingAction, PrintingRoute


class ExcludeState(StateKey):
    __key_namespace__ = "ui.pages.exclude_model.state.ExcludeState"
    OBJECTS = state(list, default=[])
    ROWS = state(list, default=[])
    COUNT = state(int, default=0)
    EMPTY = state(bool, default=False)
    NO_GEOMETRY = state(bool, default=False)
    PAGINATED = state(bool, default=False)
    PAGE = state(str, default="1 / 1")
    PREVIOUS = state(str, default="disabled")
    NEXT = state(str, default="disabled")
    REQUEST = state(str, default="disabled")
    REQUEST_LABEL = state(str, default="EXCLUDE SELECTED")


class ExcludeCommand(CommandKey):
    SELECT = "exclude.select"


def object_action(name):
    return Command(ExcludeCommand.SELECT, name).wire_id


def object_style(selected, excluded):
    """What an object's card and footprint show for its selection."""
    return dict(
        text_color=ThemeColor.DIM if excluded else ThemeColor.SECONDARY if selected else ThemeColor.PRIMARY,
        border=ThemeColor.SECONDARY if selected else ThemeColor.DIM if excluded else ThemeColor.PRIMARY,
        card_background=ThemeColor.PANEL if excluded else ThemeColor.BACKGROUND,
        footprint_background=ThemeColor.PANEL if selected or excluded else ThemeColor.BACKGROUND,
        line_width=4 if selected else 2,
    )


SELECT = ItemCommand(ExcludeCommand.SELECT, payload=item("name"))
SAMPLE = dict(name="Sample_Model", kind="object", label="01  Sample Model",
              note="TAP TO SELECT", marker="1", rectangle=[0.15, 0.2, 0.4, 0.35],
              **object_style(False, False))

OBJECT_CARD = Template(
    "object",
    Frame(
        Overlay(
            Column(
                Text(item("label"), font="JetBrainsMono Bold 8pt", color=item("text_color"),
                     truncate=True).height(20).ref("name"),
                Text(item("note"), font="JetBrainsMono 8pt", color=ThemeColor.DIM,
                     truncate=True).height(20).ref("status"),
                gap=8,
            ).height(48).align(vertical="center").margin(left=12, right=12).ref("caption"),
            Hitbox(SELECT).ref("select"),
        ),
        border=item("border"),
        background=item("card_background"),
        line_width=2,
    ).height(60).ref("card"),
    sample=SAMPLE,
)

FOOTPRINT = Template(
    "object",
    PlacedFrame(
        Overlay(
            Text(item("marker"), font="JetBrainsMono Bold 10pt", truncate=True, fit=True,
                 color=item("text_color")).ref("number"),
            Hitbox(SELECT).ref("select"),
        ),
        rectangle=item("rectangle"),
        border=item("border"),
        background=item("footprint_background"),
        line_width=item("line_width"),
    ).ref("footprint"),
    sample=SAMPLE,
)

def create_page():
    page = PageTree(
        Column(
            Grid(matrix=((Button(PrintingAction(PrintingRoute.EXCLUDE_BACK), "BACK",
                                font="JetBrainsMono Bold 8pt"), Spacer()),),
                 columns=(146, FLEX), rows=Equal(1), gap=12)
            .padding(left=14, top=6, right=14, bottom=2).height(56).ref("exclude.header"),
            Grid(
                matrix=((
                    Frame(Overlay(
                        StateCase(
                            ListView(items=bind(ExcludeState.OBJECTS), item_key=item("name"),
                                     template_key=item("kind"), templates=(FOOTPRINT,), direction="overlay")
                            .ref("exclude.footprints"),
                            selector=bind(ExcludeState.NO_GEOMETRY), expected=False,
                        ),
                        StateCase(Text("Geometry unavailable", color=ThemeColor.DIM,
                                       font="JetBrainsMono 8pt", truncate=True),
                                  selector=bind(ExcludeState.NO_GEOMETRY), expected=True),
                    ), border=ThemeColor.BORDER, background=ThemeColor.BACKGROUND)
                    .ref("exclude.bed"),
                    Column(
                        Text(derived(lambda count: "OBJECTS  %d" % count, bind(ExcludeState.COUNT)),
                             font="JetBrainsMono Bold 8pt", horizontal="left")
                        .height(20).ref("exclude.count"),
                        Overlay(
                            StateCase(
                                ListView(items=bind(ExcludeState.ROWS), item_key=item("name"),
                                         template_key=item("kind"), templates=(OBJECT_CARD,), gap=8)
                                .ref("exclude.rows"),
                                selector=bind(ExcludeState.EMPTY), expected=False,
                            ),
                            StateCase(Text("No objects defined in this file", color=ThemeColor.DIM,
                                           truncate=True), selector=bind(ExcludeState.EMPTY), expected=True),
                        ),
                        StateCase(
                            Grid(matrix=((
                                Button(PrintingAction(PrintingRoute.EXCLUDE_PREV), "PREV",
                                       state=bind(ExcludeState.PREVIOUS), font="JetBrainsMono Bold 8pt"),
                                Text(bind(ExcludeState.PAGE), color=ThemeColor.DIM),
                                Button(PrintingAction(PrintingRoute.EXCLUDE_NEXT), "NEXT",
                                       state=bind(ExcludeState.NEXT), font="JetBrainsMono Bold 8pt"),
                            ),), columns=(90, FLEX, 90), rows=Equal(1), gap=8),
                            selector=bind(ExcludeState.PAGINATED), expected=True,
                        ).height(32).ref("exclude.pagination"),
                        Button(PrintingAction(PrintingRoute.EXCLUDE_REQUEST), bind(ExcludeState.REQUEST_LABEL),
                               state=bind(ExcludeState.REQUEST), font="JetBrainsMono Bold 9pt")
                        .height(56).ref("exclude.request"),
                        gap=12,
                    ).ref("exclude.controls"),
                ),), columns=(350, FLEX), rows=Equal(1), gap=24,
            ).margin(left=20, right=20, top=18, bottom=18).ref("exclude.body"),
            gap=0,
        ).ref("exclude.root"), Rect(0, 0, 800, 442), page_id=AppPage.EXCLUDE_MODEL,
    )
    page.title = "Exclude Model"
    return page


PAGE = create_page()
