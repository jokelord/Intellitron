from fractions import Fraction


def test_wayland_ratio_uses_full_region_extent_and_only_corrects_upper_endpoint():
    from backend.computer_use.coordinates import ratio_to_wayland

    assert ratio_to_wayland(Fraction(1, 2), Fraction(1, 2), (10, 20), 1920, 1200) == (970.0, 620.0)
    assert ratio_to_wayland(Fraction(0), Fraction(0), (10, 20), 1920, 1200) == (10.0, 20.0)
    assert ratio_to_wayland(Fraction(1), Fraction(1), (10, 20), 1920, 1200) == (1929.0, 1219.0)

import pytest


def test_crop_uses_independent_axis_baselines_for_rectangular_desktop():
    from backend.computer_use.coordinates import crop_rect, local_extent, ratio_to_x11

    assert crop_rect(3840, 2160, 0, 0, 2) == (0, 0, 960, 540)
    # A centered zoom-2 crop spans 500 normalized units on each axis, but the
    # physical crop remains 1920x1200 on a 3840x2400 monitor.
    rect = crop_rect(3840, 2400, 500, 500, 2)
    assert rect == (960, 600, 2880, 1800)
    assert (rect[2] - rect[0], rect[3] - rect[1]) == (1920, 1200)
    assert local_extent(1920, 1200, 3840, 2400) == (Fraction(1000), Fraction(1000))
    assert ratio_to_x11(1, 1, (-1920, 0), 1920, 1080) == (-1, 1079)


def test_edge_crop_keeps_exact_source_rectangle_with_independent_local_axes():
    from backend.computer_use.coordinates import crop_rect, local_extent

    rect = crop_rect(3840, 2400, 500, 10, 2)
    assert rect == (960, 0, 2880, 624)
    physical_width = rect[2] - rect[0]
    physical_height = rect[3] - rect[1]
    assert local_extent(physical_width, physical_height, 3840, 2400) == (Fraction(1000), Fraction(1000))
    assert (physical_width, physical_height) == (1920, 624)


def test_edge_local_center_maps_to_source_1920_312_regardless_of_square_side(crop):
    from backend.computer_use.coordinates import local_to_monitor

    for side in (1024, 1536, 3840):
        item = crop(source_width=3840, source_height=2400,
                    x0=960, y0=0, x1=2880, y1=624,
                    detail_width=side, detail_height=side,
                    model_width=side, model_height=side)
        assert local_to_monitor(item, 500, 500) == (Fraction(1, 2), Fraction(13, 100))


def test_crop_uses_floor_ceil_enclosure_not_scalar_rounding():
    from backend.computer_use.coordinates import crop_rect

    assert crop_rect(7, 5, 500, 500, 4) == (2, 1, 5, 4)


def test_local_to_monitor_uses_exact_integer_crop(crop):
    from backend.computer_use.coordinates import local_to_monitor

    item = crop(
        x0=100,
        y0=200,
        x1=500,
        y1=600,
        source_width=800,
        source_height=900,
        local_width=Fraction(1000),
        local_height=Fraction(1000),
    )
    assert local_to_monitor(item, item.local_width / 2, item.local_height / 2) == (
        Fraction(3, 8),
        Fraction(4, 9),
    )


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -0.1, 1.1])
def test_ratio_mapping_rejects_invalid_numeric_values(bad):
    from backend.computer_use.coordinates import ratio_to_x11

    with pytest.raises((TypeError, ValueError)):
        ratio_to_x11(bad, 0.5, (0, 0), 10, 10)


def test_half_up_positive_is_not_bankers_rounding():
    from backend.computer_use.coordinates import half_up_positive

    assert half_up_positive(Fraction(5, 2)) == 3
    assert half_up_positive(Fraction(7, 2)) == 4
