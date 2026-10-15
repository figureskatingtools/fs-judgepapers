"""processor.segment_intro_page — which page (if any) precedes a segment's
papers in a personal packet."""
import pytest

from processor import segment_intro_page

PREFIX = "SEN----------WOMEN--------QUAL0001"


@pytest.mark.parametrize("options, expected", [
    ({}, "schedule"),
    ({"globalSegmentCover": False}, "schedule"),
    ({"globalSegmentCover": True}, "cover"),
    ({"globalSegmentCover": False, "segmentCovers": {PREFIX: True}}, "cover"),
    ({"globalSegmentCover": True, "segmentCovers": {PREFIX: False}}, "schedule"),
    ({"globalSegmentCover": True, "segmentCovers": {"OTHER": False}}, "cover"),
])
def test_cover_or_schedule(options, expected):
    assert segment_intro_page(options, PREFIX) == expected


@pytest.mark.parametrize("options, expected", [
    ({"globalOmitSegmentCover": True}, "none"),
    ({"globalOmitSegmentCover": True, "globalSegmentCover": True}, "none"),
    ({"globalOmitSegmentCover": True, "segmentCovers": {PREFIX: True}}, "none"),
    ({"omitSegmentCovers": {PREFIX: True}}, "none"),
    ({"omitSegmentCovers": {"OTHER": True}}, "schedule"),
    ({"globalOmitSegmentCover": True, "omitSegmentCovers": {PREFIX: False},
      "globalSegmentCover": True}, "cover"),
    ({"globalOmitSegmentCover": False}, "schedule"),
])
def test_omit(options, expected):
    assert segment_intro_page(options, PREFIX) == expected


@pytest.mark.parametrize("options", [
    {"globalOmitSegmentCover": "true"},
    {"globalOmitSegmentCover": 1},
    {"omitSegmentCovers": [PREFIX]},
    {"omitSegmentCovers": "all"},
])
def test_omit_needs_a_real_true(options):
    # Only a JSON `true` drops the page; anything else keeps today's behaviour.
    assert segment_intro_page(options, PREFIX) == "schedule"
