## Retained file-browser controls for the current page.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from dataclasses import dataclass

from ui import Button, Column, Overlay, PageTree, Rect, Text, ThemeColor
from ui.actions import Action
from ui.renderer import SCREEN_HEIGHT, SCREEN_WIDTH

from .keys import AppPage


@dataclass(frozen=True)
class FileRowAction(Action):
    index: int
    kind = "file_row"

    def __post_init__(self):
        if type(self.index) is not int or self.index < 0:
            raise ValueError("File row requires a non-negative index")

    @property
    def wire_id(self):
        return "file.item%d" % self.index

    def as_dict(self):
        return {"kind": self.kind, "index": self.index}


def content_bounds(renderer):
    width = getattr(renderer, "screen_width", SCREEN_WIDTH)
    height = getattr(renderer, "screen_height", SCREEN_HEIGHT)
    return Rect(30, 62, width - 60, height - 164)


def create_page(row_count, bounds):
    # Slots represent page positions, not file identities. The browser owns
    # the file snapshot and resolves existing file.itemN actions against it.
    slots = Column(*(Button(FileRowAction(index), "", font="JetBrainsMono 12pt")
                     .grow().ref("files.slot.%d" % index) for index in range(row_count)), gap=9)
    empty = Text("No G-code files", color=ThemeColor.DIM, font="Roboto 16pt")\
        .margin(top=20).ref("files.empty")
    return PageTree(Overlay(slots, empty), bounds, page_id=AppPage.FILE_BROWSER)


def draw(page, renderer, pagination):
    visible = pagination.visible
    slots = tuple(page.node("files.slot.%d" % index) for index in range(len(visible)))
    for node, entry in zip(slots, visible):
        node.label = entry.name + "  >" if entry.directory else entry.name
    refs = tuple("files.slot.%d" % index for index in range(len(visible))) if slots else ("files.empty",)
    foreground = slots if slots else (page.node("files.empty"),)
    # begin_page() owns the cleared background and fresh input generation.
    return page.draw(renderer, reuse_layout=True, refs=refs, render_from=foreground)
