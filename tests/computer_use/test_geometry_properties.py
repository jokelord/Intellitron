import random
from fractions import Fraction


def test_ten_thousand_deterministic_geometry_cases():
    from backend.computer_use.coordinates import crop_rect, local_extent

    rng = random.Random(0xC0FFEE)
    zooms = (2, 4, 8, 16)
    for index in range(10_000):
        width = rng.randint(1, 8192)
        height = rng.randint(1, 8192)
        x = rng.randint(0, 1000)
        y = rng.randint(0, 1000)
        zoom = zooms[index % len(zooms)]
        x0, y0, x1, y1 = crop_rect(width, height, x, y, zoom)
        assert 0 <= x0 < x1 <= width
        assert 0 <= y0 < y1 <= height
        lw, lh = local_extent(x1 - x0, y1 - y0, width, height)
        assert (lw, lh) == (1000, 1000)


def test_rectangular_source_crop_bounds_cover_each_corner_in_both_orientations():
    from backend.computer_use.coordinates import crop_rect

    for width, height in ((3840, 2400), (2400, 3840)):
        for x, y in ((0, 0), (1000, 0), (0, 1000), (1000, 1000)):
            x0, y0, x1, y1 = crop_rect(width, height, x, y, 2)
            assert 0 <= x0 < x1 <= width
            assert 0 <= y0 < y1 <= height
            assert (x0 == 0) == (x == 0)
            assert (y0 == 0) == (y == 0)
            assert (x1 == width) == (x == 1000)
            assert (y1 == height) == (y == 1000)


def test_x11_mapping_is_monotone_with_signed_origins():
    from backend.computer_use.coordinates import ratio_to_x11

    prior = None
    for step in range(1001):
        point = ratio_to_x11(Fraction(step, 1000), Fraction(step, 1000), (-4096, -2160), 8192, 4320)
        if prior is not None:
            assert point[0] >= prior[0]
            assert point[1] >= prior[1]
        prior = point
