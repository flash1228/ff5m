## Generate native-size one-bit control icons; never run on the printer.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

from pathlib import Path

from .fxi1_assets import encode


OUTPUT = Path(__file__).resolve().parents[2] / ".py/klipper/plugins/ff5m_ui/assets/controls"


def checkmark():
    # Equal-width 45-degree arms and square ends at the native 24-pixel size.
    outline = ((3, 11), (8, 16), (20, 4), (22, 6), (8, 20), (1, 13))
    pixels = bytearray(24 * 24)
    for y in range(24):
        for x in range(24):
            inside = False
            px, py = x + .5, y + .5
            for a, b in zip(outline, outline[1:] + outline[:1]):
                if (a[1] > py) != (b[1] > py) and px < (b[0] - a[0]) * (py - a[1]) / (b[1] - a[1]) + a[0]:
                    inside = not inside
            pixels[y * 24 + x] = int(inside)
    return encode(24, 24, pixels, 2)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    blob = checkmark()
    (OUTPUT / "check.fxi1").write_bytes(blob)
    print("check.fxi1: %d bytes" % len(blob))


if __name__ == "__main__":
    main()
