## Shared presentation definitions for model preview views.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from enum import Enum

from ui import Fill, Overlay, StateCase, Text, ThemeColor
from ui.bindings import bind, derived, state
from ui.components import Image
from ui.identity import StateKey


FONT = "JetBrainsMono 8pt"
BUTTON_FONT = "JetBrainsMono Bold 8pt"


class PreviewState(StateKey):
    __key_namespace__ = "ff5m_ui.model_preview.PreviewState"
    FILENAME = state(str, default="")
    BANDS = state(tuple, default=())
    PLACEHOLDER = state(str, default="PREPARING...")
    ERROR = state(str, default="")
    STYLE = state(int, default=4, minimum=0, maximum=4)
    CACHE = state(bool, default=True)
    ROTATION = state(bool, default=False)
    BUILD = state(bool, default=False)
    SCALE = state(float, default=1.0, minimum=0.25, maximum=3.0, simulation_role="preview.scale")
    YAW = state(float, default=0.0, minimum=-150.0, maximum=150.0, simulation_role="preview.yaw")
    PITCH = state(float, default=30.0, minimum=-70.0, maximum=70.0, simulation_role="preview.pitch")
    STATUS = state(str, default="")
    METRICS = state(str, default="")
    DETAIL = state(str, default="")
    FILE_TIME = state(str, default="--")
    FILE_LAYERS = state(str, default="--")
    FILE_SIZE = state(str, default="--")


class PreviewRef(Enum):
    ROOT = "model_preview.root"
    FILENAME = "model_preview.filename"
    CAMERA = "model_preview.camera"
    IMAGE = "model_preview.image"
    STATUS = "model_preview.status"
    METRICS = "model_preview.metrics"
    SCALE = "model_preview.scale"
    INFO = "model_preview.info"
    YAW_PAD = "model_preview.yaw_pad"
    TILT_PAD = "model_preview.tilt_pad"
    ERROR = "model_preview.error"


class ModelFrame(Image):
    """One opaque raster, transported in bounded FXI1 image bands."""

    covers_bounds = True


def model_frame():
    """The raster, or what the page shows until there is one."""
    return Overlay(
        ModelFrame(bind(PreviewState.BANDS)),
        StateCase(Fill(ThemeColor.PANEL), Text(bind(PreviewState.PLACEHOLDER), ThemeColor.DIM, FONT),
                  selector=derived(bool, bind(PreviewState.PLACEHOLDER)), expected=True))


def _selected(value):
    return "selected" if value else "enabled"


def _style_state(current, index):
    return _selected(current == index)
