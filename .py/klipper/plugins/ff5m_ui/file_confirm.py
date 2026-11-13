## The page that confirms a print and its one-print mesh choices.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from ui import Button, Overlay, PageTree, Rect, StateCase, StateKey, Text, ThemeColor, bind, state
from ui.components import ToggleSwitch
from ui.header import WireAction
from ui.layout_helpers import centered_button_row
from ui.renderer import HEADER_BOTTOM, SCREEN_HEIGHT, SCREEN_WIDTH

from .keys import AppPage


BUTTON_FONT = "Roboto Bold 16pt"
OPTION_LABEL_WIDTH = 620


class ConfirmState(StateKey):
    NAME = state(str, default="")
    SIZE = state(str, default="")
    HINT = state(str, default="")
    REBUILD = state(bool, default=False)
    AUTO = state(bool, default=False)


def _placed(node, x, y, width, height):
    return node.offset(x, y - HEADER_BOTTOM).size(width, height)


def _option(action, y, label, subtitle, active):
    return (
        _placed(Text(label, ThemeColor.PRIMARY, "JetBrainsMono Bold 8pt", horizontal="left"),
                44, y, OPTION_LABEL_WIDTH, 24),
        _placed(Text(subtitle, ThemeColor.DIM, "JetBrainsMono 8pt", horizontal="left"),
                44, y + 22, OPTION_LABEL_WIDTH, 24),
        _placed(ToggleSwitch(WireAction(action), active), 679, y + 5, 76, 38),
    )


def create_page(renderer, repeat):
    labels = ("PREVIEW", "PRINT AGAIN" if repeat else "START PRINT")
    font = renderer.normalize_font(BUTTON_FONT)
    padding = renderer.BUTTON_TEXT_PADDING
    bounds = centered_button_row(
        labels, 0, 316, SCREEN_WIDTH, measure_text=renderer.text_width, font=font, height=96, padding=padding,
        minimum=max(renderer.text_width(label, font) for label in labels) + 2 * padding, maximum=760, gap=18,
        margin=24)
    buttons = (_placed(Button(WireAction(action), label, font=font), *box)
               for action, label, box in zip(("file.preview", "file.start"), labels, bounds))
    return PageTree(Overlay(
        _placed(Text(bind(ConfirmState.NAME), ThemeColor.BRIGHT, BUTTON_FONT, truncate=True), 40, 76, 720, 40),
        _placed(Text(bind(ConfirmState.SIZE), ThemeColor.PRIMARY, "Roboto 12pt"), 0, 120, SCREEN_WIDTH, 40),
        *_option("file.mesh.rebuild", 170, "REBUILD BED MESH", bind(ConfirmState.HINT), bind(ConfirmState.REBUILD)),
        StateCase(*_option("file.mesh.auto", 238, "SAVE MESH FOR FUTURE PRINTS", "YOU'LL BE ASKED AFTER PRINT",
                           bind(ConfirmState.AUTO)),
                  selector=bind(ConfirmState.REBUILD), expected=True),
        *buttons,
    ), Rect(0, HEADER_BOTTOM, SCREEN_WIDTH, SCREEN_HEIGHT - HEADER_BOTTOM), page_id=AppPage.FILE_CONFIRM)
