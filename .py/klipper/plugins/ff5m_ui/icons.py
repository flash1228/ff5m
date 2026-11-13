## Immutable indexed icon sets with palette-only theme updates.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from functools import lru_cache
import os
import struct


@lru_cache(maxsize=8)
def load(set_name, name):
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        "assets", set_name, name + ".fxi1")
    with open(path, "rb") as stream:
        blob = stream.read(1025)
    invalid = "Invalid FXI1 icon: %s/%s" % (set_name, name)
    if len(blob) < 20 or len(blob) > 1024:
        raise ValueError(invalid)
    magic, version, compression, colors, bpp, width, height, raw, size = struct.unpack_from(
        "<4sBBBBHHII", blob)
    if (magic != b"FXI1" or version != 1 or compression != 0
            or colors not in (2, 4) or bpp != (1 if colors == 2 else 2)
            or not 0 < width <= 64 or not 0 < height <= 64
            or raw != (width * height * bpp + 7) // 8
            or size != raw or len(blob) != 20 + colors * 4 + size):
        raise ValueError(invalid)
    return blob


def colored(renderer, set_name, name, colors):
    blob = load(set_name, name)
    palette = (0,) + tuple(0xff000000 | int(renderer.color(color), 16) for color in colors)
    if len(palette) != blob[6]:
        raise ValueError("FXI1 icon palette mismatch: %s/%s" % (set_name, name))
    return blob[:20] + struct.pack("<%dI" % len(palette), *palette) + blob[20 + 4 * len(palette):]


def render(renderer, set_name, name, x, y, colors):
    return renderer.image(x, y, colored(renderer, set_name, name, colors))
