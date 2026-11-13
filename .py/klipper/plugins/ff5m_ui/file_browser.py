## The file browser page: a list or tiles of the current page of files.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from dataclasses import dataclass

from ui import (
    Button, ButtonSurface, Column, Fill, Hitbox, Image, Overlay, PageTree, Panel, Rect, StateCase, StateKey, Stroke,
    Text, ThemeColor, ThemeRole, bind, state,
)
from ui.actions import Action
from ui.header import WireAction
from ui.renderer import SCREEN_HEIGHT, SCREEN_WIDTH

from .keys import AppPage


LIST_BOUNDS = (30, 62, 740, 316)
LIST_FONT = "JetBrainsMono 12pt"
LIST_ROWS = 5
TILE_X = (22, 280, 538)
TILE_Y = 68
TILE_WIDTH = 240
TILE_HEIGHT = 306
TILE_IMAGE_Y = 78 - TILE_Y
TILE_TEXT_X = 14
TILE_TEXT_WIDTH = TILE_WIDTH - 2 * TILE_TEXT_X
TILE_LABEL_Y = 266 - TILE_Y
TILE_DETAIL_Y = 352 - TILE_Y
FOLDER_WIDTH = 112
FOLDER_HEIGHT = 72
HEADER_FONT = "Roboto Bold 8pt"
SMALL_FONT = "JetBrainsMono 8pt"


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


class FileState(StateKey):
    __key_namespace__ = "ui.pages.file_browser"
    LIST_BUTTON = state(str, default="selected")
    GRID_BUTTON = state(str, default="enabled")
    EMPTY = state(bool, default=False)
    PAGE = state(str, default="1 / 1")
    HAS_PREVIOUS = state(bool, default=False)
    HAS_NEXT = state(bool, default=False)


def _row_state(index):
    class RowState(StateKey):
        __key_namespace__ = "ui.pages.file_browser.row%d" % index
        SHOWN = state(bool, default=False)
        LABEL = state(str, default="")
    return RowState


def _tile_state(index):
    class TileState(StateKey):
        __key_namespace__ = "ui.pages.file_browser.tile%d" % index
        SHOWN = state(bool, default=False)
        # What the tile shows above its name: image, loading, folder or none.
        CONTENT = state(str, default="none")
        IMAGE = state(tuple, default=())
        LABEL = state(str, default="")
        DETAIL = state(str, default="")
    return TileState


ROWS = tuple(_row_state(index) for index in range(LIST_ROWS))
TILES = tuple(_tile_state(index) for index in range(len(TILE_X)))


@dataclass(frozen=True)
class TileView:
    """What one tile shows: its content (TileState.CONTENT), the raster of an image and its two lines."""
    content: str
    image: bytes
    label: str
    detail: str


def _placed(node, x, y, width, height):
    return node.offset(x, y).size(width, height)


def _case(keys, content, *nodes):
    return StateCase(*nodes, selector=bind(keys.CONTENT), expected=content)


def _tile_note(text, image_height):
    return _placed(Text(text, ThemeColor.DIM, SMALL_FONT), 0, TILE_IMAGE_Y, TILE_WIDTH, image_height)


def _folder(image_height):
    x = (TILE_WIDTH - FOLDER_WIDTH) // 2
    y = TILE_IMAGE_Y + (image_height - FOLDER_HEIGHT) // 2 + 6
    body = (x, y + 12, FOLDER_WIDTH, FOLDER_HEIGHT - 12)
    return (_placed(Fill(ThemeColor.PRIMARY), x, y, 48, 14),
            _placed(Fill(ThemeColor.PANEL), *body),
            _placed(Stroke(ThemeColor.PRIMARY, 2), *body))


def _tile_face(keys, preview_size, pressed):
    image_width, image_height = preview_size
    image_x = (TILE_WIDTH - image_width) // 2
    image_box = (image_x, TILE_IMAGE_Y, image_width, image_height)
    return Overlay(
        Panel(ThemeColor.BRIGHT if pressed else ThemeRole.BUTTON_BORDER,
              ThemeColor.PRESSED_BACKGROUND if pressed else ThemeRole.BUTTON_BACKGROUND, 3 if pressed else 2),
        _case(keys, "image", _placed(Image(bind(keys.IMAGE)), *image_box)),
        _case(keys, "loading", _placed(Panel(ThemeColor.BORDER, ThemeColor.PANEL, 1), *image_box),
              _tile_note("LOADING...", image_height)),
        _case(keys, "folder", *_folder(image_height)),
        _case(keys, "none", _tile_note("NO PREVIEW", image_height)),
        _placed(Text(bind(keys.LABEL), ThemeColor.BRIGHT, "Roboto Bold 12pt", horizontal="left", vertical="top",
                     max_height=68, wrap=True, truncate=True), TILE_TEXT_X, TILE_LABEL_Y, TILE_TEXT_WIDTH, 68),
        _placed(Text(bind(keys.DETAIL), ThemeColor.DIM, SMALL_FONT, horizontal="left", truncate=True),
                TILE_TEXT_X, TILE_DETAIL_Y - 12, TILE_TEXT_WIDTH, 24),
    )


def _tile(index, keys, preview_size):
    surface = ButtonSurface(FileRowAction(index), _tile_face(keys, preview_size, False),
                            _tile_face(keys, preview_size, True))
    # Placed itself, so that a tile's change repaints this tile alone.
    return _placed(StateCase(surface, selector=bind(keys.SHOWN), expected=True),
                   TILE_X[index], TILE_Y, TILE_WIDTH, TILE_HEIGHT)


def _row(index, keys):
    # A hidden slot keeps its place: page positions, not file identities.
    return StateCase(Button(FileRowAction(index), bind(keys.LABEL), font=LIST_FONT),
                     selector=bind(keys.SHOWN), expected=True).grow()


def _page(*content):
    return PageTree(Overlay(
        _placed(Hitbox(WireAction("file.model_preview.tap")), 170, 7, 370, 46),
        _placed(Button(WireAction("file.view.list"), "LIST", state=bind(FileState.LIST_BUTTON), font=HEADER_FONT),
                548, 7, 70, 46),
        _placed(Button(WireAction("file.view.tiles"), "GRID", state=bind(FileState.GRID_BUTTON), font=HEADER_FONT),
                622, 7, 74, 46),
        _placed(Button(WireAction("file.refresh"), "SCAN", font=HEADER_FONT), 700, 7, 86, 46),
        StateCase(_placed(Text("No G-code files", ThemeColor.DIM, "Roboto 16pt"), 30, 82, 740, 296),
                  selector=bind(FileState.EMPTY), expected=True),
        *content,
        _placed(Button(WireAction("file.prev"), "< PAGE", active=bind(FileState.HAS_PREVIOUS)), 210, 384, 150, 50),
        _placed(Text(bind(FileState.PAGE), ThemeColor.BRIGHT, "Roboto 8pt"), 360, 384, 80, 50),
        _placed(Button(WireAction("file.next"), "PAGE >", active=bind(FileState.HAS_NEXT)), 440, 384, 150, 50),
    ), Rect(0, 0, SCREEN_WIDTH, SCREEN_HEIGHT), page_id=AppPage.FILE_BROWSER)


def create_list_page():
    return _page(_placed(Column(*(_row(index, keys) for index, keys in enumerate(ROWS)), gap=9), *LIST_BOUNDS))


def create_tile_page(preview_size):
    return _page(*(_tile(index, keys, preview_size) for index, keys in enumerate(TILES)))


def _page_values(view, pagination):
    return {
        FileState.LIST_BUTTON: "selected" if view == "list" else "enabled",
        FileState.GRID_BUTTON: "selected" if view == "tiles" else "enabled",
        FileState.EMPTY: not pagination.visible,
        FileState.PAGE: "%d / %d" % (pagination.page + 1, pagination.page_count),
        FileState.HAS_PREVIOUS: pagination.has_previous,
        FileState.HAS_NEXT: pagination.has_next,
    }


def list_values(pagination):
    """The state of a page of files as rows."""
    values = _page_values("list", pagination)
    entries = pagination.visible
    for index, keys in enumerate(ROWS):
        shown = index < len(entries)
        label = "" if not shown else entries[index].name + ("  >" if entries[index].directory else "")
        values.update({keys.SHOWN: shown, keys.LABEL: label})
    return values


def tile_values(pagination, tiles):
    """The state of a page of files as tiles showing these TileViews."""
    values = _page_values("tiles", pagination)
    for index, keys in enumerate(TILES):
        tile = tiles[index] if index < len(tiles) else TileView("none", None, "", "")
        values.update({keys.SHOWN: index < len(tiles), keys.CONTENT: tile.content,
                       keys.IMAGE: ((0, 0, tile.image),) if tile.image else (),
                       keys.LABEL: tile.label, keys.DETAIL: tile.detail})
    return values
