# Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
# This file may be distributed under the terms of the GNU GPLv3 license.

import time

from ui import FLEX, Button, Column, Frame, Grid, PageTree, Rect, Row, Spacer, Text
from ui.bindings import bind, derived, state
from ui.identity import StateKey

from ..keys import AppPage
from ..styles import UI_STYLES, UiStyle
from .actions import ShaperAction


class StartState(StateKey):
    __key_namespace__ = "ff5m_ui.shaper.start"
    # ShaperSession owns the choice and writes it before the page is shown.
    SWEEP = state(bool, default=False)
    HAS_LAST = state(bool, default=False)
    SUMMARY = state(str, default="No saved calibration")


def _method(label, sweep):
    """The button of one method: sweep says which method it is, not which one is chosen."""
    return Button(ShaperAction("vibrations", label.lower()), label,
                  state=derived(lambda chosen: "selected" if chosen == sweep else "enabled",
                                bind(StartState.SWEEP))) \
        .style(UiStyle.BUTTON_BOLD_8).ref("shaper.start.vibrations." + label.lower())


def _method_note(sweep):
    if sweep:
        return "Klipper 0.13. Needs 20 mm each side."
    return "Klipper 0.11. Short pulses in place."


root = Column(
    Frame(Column(
        Text("BEFORE YOU START").style(UiStyle.PRIMARY_LEFT).height(22),
        Text("Remove prints and objects from the bed.", wrap=True).style(UiStyle.TEXT_LEFT),
        gap=6,
    )).style(UiStyle.FRAME_PANEL).height(82).padding(left=20, right=20, top=16, bottom=16),
    Spacer().height(20),
    Row(
        Text("VIBRATIONS").width("content").style(UiStyle.TEXT_LEFT).ref("shaper.start.vibrations.label"),
        Spacer(),
        _method("STANDARD", False),
        _method("SWEEPING", True),
        gap=8,
    ).height(48),
    Spacer().height(12),
    Text(derived(_method_note, bind(StartState.SWEEP))).style(UiStyle.DIM_LEFT)
        .height(24).ref("shaper.start.vibrations.note"),
    Spacer().height(20),
    Button(ShaperAction("start"), "START CALIBRATION")
        .style(UiStyle.BUTTON_BOLD_12).height(56).ref("shaper.start.measure"),
    Spacer().height(20),
    Grid(matrix=((
        Button(ShaperAction("last"), "LAST RESULTS",
               state=derived(lambda ready: "enabled" if ready else "disabled", bind(StartState.HAS_LAST)))
            .style(UiStyle.BUTTON_BOLD_8).ref("shaper.start.last"),
        Text(bind(StartState.SUMMARY), truncate=True).style(UiStyle.DIM_LEFT).ref("shaper.start.summary"),
    ),), columns=(FLEX, FLEX), rows=(FLEX,), gap=(12, 0)).height(44),
    Spacer(),
).padding(left=40, top=18, right=40, bottom=18) \
    .ref("shaper.start.root")

PAGE = PageTree(root, Rect(0, 56, 800, 386), page_id=AppPage.SHAPER_START,
                state_schema=tuple(StartState), styles=UI_STYLES)
PAGE.title = "Input shaper"
PAGE.show_back = True


def values(latest, sweep):
    """Summarize the latest history entry; its spectra are not loaded."""
    summary = "No saved calibration"
    if latest is not None:
        date = time.strftime("%Y-%m-%d %H:%M", time.localtime(latest["saved_at"]))
        summary = " / ".join(axis.upper() for axis in latest["axes"]) + "  |  " + date
    return {StartState.SWEEP: sweep, StartState.HAS_LAST: latest is not None, StartState.SUMMARY: summary}
