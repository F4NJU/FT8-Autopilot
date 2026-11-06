from PySide6.QtCore import QRectF

from wsjtx_autopilot.gui.session_map import _world_geometries, fit_world_viewport, project_equirectangular


def test_equirectangular_projection_places_centre_at_origin() -> None:
    projected = project_equirectangular(0, 0, 720, 360)

    assert projected.x() == 360
    assert projected.y() == 180


def test_equirectangular_projection_places_north_west_in_top_left() -> None:
    projected = project_equirectangular(90, -180, 720, 360)

    assert projected.x() == 0
    assert projected.y() == 0


def test_world_viewport_is_always_two_to_one_without_stretching() -> None:
    viewport = fit_world_viewport(QRectF(0, 0, 600, 600))

    assert viewport.width() == 600
    assert viewport.height() == 300
    assert viewport.top() == 150


def test_bundled_world_map_contains_natural_earth_country_outlines() -> None:
    polygons = _world_geometries()

    # Natural Earth 1:110m has many separate land polygons; a few generic
    # silhouettes would not meet the required map quality.
    assert len(polygons) > 150
    assert any(longitude < -150 and latitude > 50 for ring in polygons for longitude, latitude in ring[0])
    assert any(0 < longitude < 20 and 40 < latitude < 55 for ring in polygons for longitude, latitude in ring[0])
