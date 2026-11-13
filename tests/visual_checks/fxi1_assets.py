## Offline encoding for small raw indexed UI resources.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import struct


def encode(width, height, pixels, palette_size):
    bpp = 1 if palette_size <= 2 else 2
    packed = bytearray((width * height * bpp + 7) // 8)
    for offset, value in enumerate(pixels):
        packed[offset * bpp // 8] |= value << (8 - bpp - offset * bpp % 8)
    return (struct.pack("<4sBBBBHHII", b"FXI1", 1, 0, palette_size, bpp,
                        width, height, len(packed), len(packed))
            + bytes(palette_size * 4) + packed)
