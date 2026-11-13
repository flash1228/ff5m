## Half-up rounding of calibration values to a thousandth of a millimeter.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import re


# A float's shortest text (repr): digits, an optional fraction and exponent.
_WRITTEN_RE = re.compile(r"^(-?)(\d+)(?:\.(\d*))?(?:e([+-]\d+))?$")


def written_fraction(value):
    """The exact value of a number as its float is written (2.0005, not the
    binary 2.000499...), as (negative, numerator, denominator)."""
    text = repr(float(value))
    match = _WRITTEN_RE.match(text)
    if match is None:
        raise ValueError("%s is not a finite number" % text)
    sign, whole, fraction, exponent = match.groups()
    fraction = fraction or ""
    power = int(exponent or 0) - len(fraction)
    numerator = int(whole + fraction)
    if power >= 0:
        return sign == "-", numerator * 10 ** power, 1
    return sign == "-", numerator, 10 ** -power


def round_thousandths(negative, numerator, denominator):
    """numerator / denominator, both non-negative, rounded to 0.001 with a
    half away from zero; the sign is kept, also on a zero."""
    thousandths = (2000 * numerator + denominator) // (2 * denominator)
    return -(thousandths / 1000) if negative else thousandths / 1000
