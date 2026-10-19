## Visible parameters for the lazy model preview settings screen.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from ..parameter_controls import control_row_pitch


def current_view_content(state):
    values = "YAW %.1f / TILT %.1f / %.0f%%" % (state["yaw"], state["pitch"], state["scale"] * 100)
    return "DEFAULT VIEW", values, "Save current rotation, tilt and scale."


def current_view_pitch(state, row_width):
    label, _values, description = current_view_content(state)
    return control_row_pitch(label, description, row_width)


def parameter_keys(style, mode):
    keys = ["type", "cache"]
    # SOLID chooses its grid from physical model dimensions in the renderer.
    if style != "SOLID":
        keys.append("grid")
    if style == "STACKED":
        keys.append("stacked_density")
    elif style == "POINTS":
        keys.append("points_density")
    keys.extend(("yaw", "pitch", "scale", "fit", "fps"))
    if mode == "lab":
        keys.append("rotation_speed")
    return tuple("feather_preview_" + key for key in keys)
