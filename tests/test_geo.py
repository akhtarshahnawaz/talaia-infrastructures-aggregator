"""AOI parsing, banding and tiling."""
import pytest

from talaia.geo import area_km2, buffer_m, merge_bboxes, parse_aoi, tiles_for_geometry

SQUARE = {"type": "Polygon",
          "coordinates": [[[1.8, 41.7], [1.9, 41.7], [1.9, 41.8], [1.8, 41.8], [1.8, 41.7]]]}


def test_bands_are_ordered_by_arrival_not_input_order():
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"band": "6-12h", "hours": 12},
         "geometry": {"type": "Polygon", "coordinates": [[[1.7, 41.6], [2.0, 41.6],
                                                          [2.0, 41.9], [1.7, 41.9], [1.7, 41.6]]]}},
        {"type": "Feature", "properties": {"band": "0-3h", "hours": 3},
         "geometry": SQUARE},
    ]}
    aoi = parse_aoi(fc)
    assert [b.label for b in aoi.bands] == ["0-3h", "6-12h"]
    assert [b.minutes for b in aoi.bands] == [180.0, 720.0]
    assert aoi.is_banded


def test_minutes_parsed_from_label_when_absent():
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"band": "3h"}, "geometry": SQUARE}]}
    assert parse_aoi(fc).bands[0].minutes == 180.0


def test_buffer_is_metric():
    plain = parse_aoi(SQUARE)
    buffered = parse_aoi(SQUARE, buffer_metres=1000)
    # A ~8.3 x 11.1 km box grows by 1 km on every side.
    assert buffered.area_km2 > plain.area_km2
    assert 1.35 < buffered.area_km2 / plain.area_km2 < 1.60


def test_self_intersecting_geometry_is_repaired():
    bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
    assert not parse_aoi(bowtie).union.is_empty


def test_empty_and_invalid_input_rejected():
    with pytest.raises(ValueError):
        parse_aoi({"type": "FeatureCollection", "features": []})
    with pytest.raises(ValueError):
        parse_aoi({"not": "geojson"})


def test_tile_cover_follows_geometry_not_bbox():
    """A diagonal sliver should need far fewer tiles than its bounding box."""
    diagonal = {"type": "Polygon", "coordinates": [[[1.0, 41.0], [1.02, 41.0],
                                                    [1.5, 41.5], [1.48, 41.5], [1.0, 41.0]]]}
    aoi = parse_aoi(diagonal)
    geom_tiles = tiles_for_geometry(aoi.union, 0.05)
    bbox_tiles = 11 * 11
    assert len(geom_tiles) < bbox_tiles / 2


def test_merge_bboxes_reduces_request_count():
    boxes = [(x * 0.05, 41.0, (x + 1) * 0.05, 41.05) for x in range(20)]
    assert len(merge_bboxes(boxes, max_groups=4)) == 4
