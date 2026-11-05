"""方向別の期限（2026-10-14）。

出発日を迎えた系列で数えると、日本発→米西海岸は出発65〜75日前に買うのが
最良で、旧期限の52日前はもう上がり始めた後だった。米西海岸発→日本は
どの日もほぼ同じ。**日本発→米西海岸だけ期限を動かし、他は据え置く。**
"""
from datetime import date

from app.predict.curve import (JP_TO_US_WEST, DEFAULT_PARAMS, route_direction,
                               route_params)
from app.predict.engine import price_rises_after


def test_only_japan_to_the_us_west_coast_is_overridden():
    assert route_direction("NRT", "LAX") == JP_TO_US_WEST
    assert route_direction("HND", "SAN") == JP_TO_US_WEST
    # 逆方向・実測の無い路線には当てない
    assert route_direction("LAX", "NRT") is None
    assert route_direction("NRT", "ICN") is None
    assert route_direction("HND", "HNL") is None


def test_japan_to_us_west_rises_from_65_days_out():
    p = route_params("NRT", "SFO")
    assert (p.sweet_spot_start_days, p.sweet_spot_end_days) == (65, 75)


def test_other_directions_keep_the_52_day_deadline():
    for o, d in (("SAN", "NRT"), ("LAX", "HND"), ("NRT", "ICN")):
        p = route_params(o, d)
        assert p.sweet_spot_start_days == DEFAULT_PARAMS.sweet_spot_start_days == 52


def test_the_deadline_shown_follows_the_direction():
    today, dep = date(2026, 10, 14), "2027-01-14"
    jp = price_rises_after(dep, today, route_params("HND", "LAX"))
    us = price_rises_after(dep, today, route_params("LAX", "HND"))
    assert jp["days_before_departure"] == 65
    assert us["days_before_departure"] == 52
    assert jp["date"] < us["date"]


def test_the_override_is_idempotent():
    """recommend と build_trend の両方で当てても値は変わらない。"""
    once = route_params("NRT", "LAX")
    assert route_params("NRT", "LAX", once) == once
