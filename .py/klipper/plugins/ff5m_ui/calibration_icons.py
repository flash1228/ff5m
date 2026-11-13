## Small immutable FXI1 resources with the active calibration-page palette.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from ui import ThemeColor
from . import icons


def nut(renderer, center_x, center_y):
    return icons.render(renderer, "calibration", "nut", center_x - 12, center_y - 10,
                        (ThemeColor.BRIGHT, ThemeColor.DIM, ThemeColor.PANEL))


def turn_arrow(renderer, center_x, center_y, direction, color):
    name = {"CW": "cw", "CCW": "ccw"}[direction]
    return icons.render(renderer, "calibration", name, center_x - 27, center_y - 22, (color,))
