from __future__ import annotations

from types import SimpleNamespace

from features.playback.helpers import build_bookmark_marks, build_half_hour_marks


def _moment(start_ts: str, fetch_group_id: str = "g") -> SimpleNamespace:
    return SimpleNamespace(start_ts=start_ts, fetch_group_id=fetch_group_id)


def _bare(n: int) -> dict:
    return {i: {"label": ""} for i in range(n)}


def test_half_hour_marks_label_nearest_moment():
    moments = [
        _moment("2026-07-13T09:31:00"),  # ~9:30
        _moment("2026-07-13T09:45:00"),  # not near a half-hour
        _moment("2026-07-13T10:00:00"),  # 10:00
        _moment("2026-07-13T10:29:00"),  # ~10:30
    ]
    marks = build_half_hour_marks(moments, _bare(4))
    assert marks[0]["label"] == "9:30"
    assert marks[2]["label"] == "10:00"
    assert marks[3]["label"] == "10:30"
    assert marks[1]["label"] == ""  # nothing within threshold


def test_half_hour_marks_afternoon_uses_12h_format():
    marks = build_half_hour_marks([_moment("2026-07-13T15:30:00")], _bare(1))
    assert marks[0]["label"] == "3:30"


def test_half_hour_marks_skip_far_targets():
    # A single noon moment: 12:00 lands; 11:30 / 12:30 are 30 min away (> 20 threshold) → not labelled.
    marks = build_half_hour_marks([_moment("2026-07-13T12:00:00")], _bare(1))
    assert marks[0]["label"] == "12:00"


def test_bookmark_star_composes_on_a_half_hour_label():
    moments = [_moment("2026-07-13T09:30:00", fetch_group_id="bm")]
    marks = build_half_hour_marks(moments, _bare(1))
    marks = build_bookmark_marks(moments, ["bm"], marks)
    assert marks[0]["label"] == "★ 9:30"
