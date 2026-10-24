from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

from .contracts import DetailCrop, exact_number


def _dimension(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError(f"{name} must be a positive integer")
    return value


def _bounded(value: Any, name: str, lower: Fraction, upper: Fraction) -> Fraction:
    result = exact_number(value, name)
    if not lower <= result <= upper:
        raise ValueError(f"{name} must be within [{lower},{upper}]")
    return result


def half_up_positive(value: Any) -> int:
    number = exact_number(value, "value")
    if number < 0:
        raise ValueError("half_up_positive requires a non-negative value")
    return number.numerator // number.denominator + (2 * (number.numerator % number.denominator) >= number.denominator)


def crop_rect(width: int, height: int, x: float, y: float, zoom: int) -> tuple[int, int, int, int]:
    width = _dimension(width, "width")
    height = _dimension(height, "height")
    cx = _bounded(x, "x", Fraction(0), Fraction(1000))
    cy = _bounded(y, "y", Fraction(0), Fraction(1000))
    if isinstance(zoom, bool) or zoom not in {2, 4, 6, 8}:
        raise ValueError("zoom must be one of 2,4,6,8")
    half_span = Fraction(500, zoom)
    left = max(Fraction(0), cx - half_span)
    right = min(Fraction(1000), cx + half_span)
    top = max(Fraction(0), cy - half_span)
    bottom = min(Fraction(1000), cy + half_span)
    x0 = (left * width / 1000).__floor__()
    y0 = (top * height / 1000).__floor__()
    x1 = (right * width / 1000).__ceil__()
    y1 = (bottom * height / 1000).__ceil__()
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if x0 >= x1 or y0 >= y1:
        raise ValueError("crop collapsed after enclosure")
    return x0, y0, x1, y1


def local_extent(cw: int, ch: int, source_width: int, source_height: int) -> tuple[Fraction, Fraction]:
    """Return Stage-2's independent 0..1000 coordinate axes for a valid crop."""
    render_extent(cw, ch, source_width, source_height)
    return Fraction(1000), Fraction(1000)


def render_extent(cw: int, ch: int, source_width: int, source_height: int) -> tuple[Fraction, Fraction]:
    """Validate and return the exact native crop extent before square stretch."""
    cw = _dimension(cw, "cw")
    ch = _dimension(ch, "ch")
    source_width = _dimension(source_width, "source_width")
    source_height = _dimension(source_height, "source_height")
    if cw > source_width or ch > source_height:
        raise ValueError("crop dimensions cannot exceed source dimensions")
    return Fraction(cw), Fraction(ch)


def raster_extent(local_width: Any, local_height: Any, long_edge: int) -> tuple[int, int]:
    """Scale a positive width/height ratio to a raster with the requested long edge."""
    width = exact_number(local_width, "local_width")
    height = exact_number(local_height, "local_height")
    long_edge = _dimension(long_edge, "long_edge")
    if width <= 0 or height <= 0:
        raise ValueError("local dimensions must be positive")
    longest = max(width, height)
    return (
        max(1, half_up_positive(Fraction(long_edge) * width / longest)),
        max(1, half_up_positive(Fraction(long_edge) * height / longest)),
    )


def local_to_monitor(crop: DetailCrop, u: float, v: float) -> tuple[Fraction, Fraction]:
    if not isinstance(crop, DetailCrop):
        raise TypeError("crop must be DetailCrop")
    local_x = _bounded(u, "u", Fraction(0), crop.local_width)
    local_y = _bounded(v, "v", Fraction(0), crop.local_height)
    source_x = Fraction(crop.x0) + local_x * crop.crop_width / crop.local_width
    source_y = Fraction(crop.y0) + local_y * crop.crop_height / crop.local_height
    return source_x / crop.source_width, source_y / crop.source_height


def ratio_to_x11(nx: float, ny: float, origin: tuple[int, int], width: int, height: int) -> tuple[int, int]:
    rx = _bounded(nx, "nx", Fraction(0), Fraction(1))
    ry = _bounded(ny, "ny", Fraction(0), Fraction(1))
    width = _dimension(width, "width")
    height = _dimension(height, "height")
    if len(origin) != 2 or any(isinstance(v, bool) or not isinstance(v, int) for v in origin):
        raise TypeError("origin must contain signed integer coordinates")
    ix = min(width - 1, half_up_positive(rx * width))
    iy = min(height - 1, half_up_positive(ry * height))
    return origin[0] + ix, origin[1] + iy


def ratio_to_wayland(nx: float, ny: float, origin: tuple[int, int], width: int, height: int) -> tuple[float, float]:
    """Map normalized extents to the libei region, nudging only the upper edge.

    The native helper validates inclusive [origin, origin + size - 1] limits.
    Interior points use the full advertised extent, without a systematic 1 px
    compression of the whole axis.
    """
    rx = _bounded(nx, "nx", Fraction(0), Fraction(1))
    ry = _bounded(ny, "ny", Fraction(0), Fraction(1))
    width = _dimension(width, "width")
    height = _dimension(height, "height")
    if len(origin) != 2 or any(isinstance(v, bool) or not isinstance(v, int) for v in origin):
        raise TypeError("origin must contain signed integer coordinates")
    return (float(origin[0] + min(rx * width, width - 1)),
            float(origin[1] + min(ry * height, height - 1)))
