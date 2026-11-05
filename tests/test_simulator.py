"""シミュレータのテスト。

既存 flight-price-monitor の tests/ に倣い、具体値ではなく性質
（決定論性・単調性・境界値）を固定する。具体値を固定すると Phase 3.5 で
実測からパラメータを推定した瞬間に全部落ちる。
"""
import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.predict import simulator
from app.predict.curve import DEFAULT_PARAMS, CurveParams, load_params

DEPARTURE = date(2026, 10, 15)


def _shape(check_date: date, airline="ANA", stops=0, params=None):
    return simulator.price_shape("NRT", "LAX", stops, airline,
                                 DEPARTURE, check_date, params)


# ------------------------------------------------------------------
# 決定論性
# ------------------------------------------------------------------

def test_same_inputs_always_give_the_same_price():
    args = ("NRT", "LAX", 0, "ANA", DEPARTURE, date(2026, 8, 8))
    first = simulator.simulate_price(*args)
    # 何度呼んでも同じ。乱数の種は入力のハッシュで、時刻や os.urandom は使わない。
    assert all(simulator.simulate_price(*args) == first for _ in range(5))


def test_jitter_depends_on_the_itinerary():
    base = ("NRT", "LAX", 0, "ANA", DEPARTURE, date(2026, 8, 8))
    assert (simulator.simulate_price(*base, via_airport="ICN")
            != simulator.simulate_price(*base, via_airport="GMP"))


def test_price_shape_has_no_jitter():
    # 較正と予測が使うのはこちら。ゆらぎが乗ると、実在しない ±3% の
    # ギザギザを予測線に載せることになる。
    p = replace(DEFAULT_PARAMS, jitter_min=0.5, jitter_max=1.5)
    assert _shape(date(2026, 8, 8), params=p) == _shape(date(2026, 8, 8))


def test_price_shape_is_not_quantized():
    # simulate_price は5ドル単位に丸めるが、shape は丸めない。
    # 丸めたまま較正すると予測線が階段状になる。
    values = [_shape(DEPARTURE - timedelta(days=d)) for d in range(30, 90)]
    assert any(v % 5 != 0 for v in values)


# ------------------------------------------------------------------
# 境界値
# ------------------------------------------------------------------

def test_departure_day_is_priceable():
    assert _shape(DEPARTURE) is not None


def test_after_departure_is_none():
    # 値付けする対象がない。
    assert _shape(DEPARTURE + timedelta(days=1)) is None


def test_prices_are_positive_across_the_whole_window():
    for days in range(0, 400):
        value = _shape(DEPARTURE - timedelta(days=days))
        assert value is not None and value > 0


# ------------------------------------------------------------------
# 曲線の形（単調性）
# ------------------------------------------------------------------

def test_last_minute_is_the_most_expensive():
    """出発直前がいちばん高い。**曲線で唯一、実測が強く支持する性質。**

    35/41 系列で一致（符号検定 z=+4.5、価格比 -16.7%）。
    """
    assert (simulator.proximity_factor(0)
            > simulator.proximity_factor(18)
            > simulator.proximity_factor(45)
            > simulator.proximity_factor(60))


def test_booking_early_is_not_penalised():
    """**早く買っても高くならない。** 2026-08-15 の実測で分かったこと。

    旧曲線は「底値帯より前は徐々に高くなる」として 300日前を 60日前より
    12% 高くしていた。実測では出発52〜160日前が平坦で、45〜75日前と
    それ以前(76〜200日前)に差が無い（32/66系列, z=-0.2）。
    この性質が壊れると、アプリは根拠なく「待て」と言い始める。
    """
    far = simulator.proximity_factor(300)
    mid = simulator.proximity_factor(120)
    sweet = simulator.proximity_factor(60)

    assert far == pytest.approx(sweet, rel=0.01)
    assert mid == pytest.approx(sweet, rel=0.01)


def test_inside_two_weeks_prices_rise_monotonically_toward_departure():
    factors = [simulator.proximity_factor(d) for d in range(0, 15)]
    # 出発日に近づくほど高い = 日数の降順で単調増加。
    assert factors == sorted(factors, reverse=True)


def test_the_sweet_spot_is_the_cheapest_part_of_the_curve():
    sweet = simulator.proximity_factor(60)
    others = [simulator.proximity_factor(d)
              for d in range(0, 366) if not 45 <= d <= 75]
    assert sweet <= min(others)


def test_the_curve_is_continuous_at_the_sweet_spot_edges():
    # 継ぎ目で価格が飛ぶと「1日待つだけで急落」という嘘の推奨が出る。
    for boundary in (45, 75):
        inside = simulator.proximity_factor(boundary)
        outside = simulator.proximity_factor(boundary + (1 if boundary == 75 else -1))
        assert abs(inside - outside) < 0.02


def test_nonstop_costs_more_than_connecting():
    assert _shape(date(2026, 8, 8), stops=0) > _shape(date(2026, 8, 8), stops=1)


def test_lcc_is_cheaper_than_a_full_service_carrier():
    assert _shape(date(2026, 8, 8), airline="ZIPAIR") < _shape(date(2026, 8, 8), airline="ANA")


def test_unknown_airline_falls_back_to_the_default_factor():
    # 実測は "United" や "ZIPAIR Tokyo" のような表記で返るので未知になりうる。
    # 落ちずに既定係数を使う（check_date に依存しない定数なので較正が吸収する）。
    assert _shape(date(2026, 8, 8), airline="Some Unknown Air") is not None


# ------------------------------------------------------------------
# パラメータの外出し（Phase 3.5 の前提）
# ------------------------------------------------------------------

def test_curve_parameters_actually_drive_the_price():
    cheap = replace(DEFAULT_PARAMS, base_fare_usd=100.0)
    assert _shape(date(2026, 8, 8), params=cheap) < _shape(date(2026, 8, 8))


def test_the_sweet_spot_window_is_configurable():
    shifted = replace(DEFAULT_PARAMS,
                      sweet_spot_start_days=100, sweet_spot_end_days=140)
    assert simulator.proximity_factor(120, shifted) == shifted.sweet_spot_factor


def test_params_json_overrides_only_what_it_names(tmp_path):
    path = tmp_path / "curve.json"
    path.write_text(json.dumps({
        "base_fare_usd": 999.0,
        "seasonality": {"10": 2.0},
        "unknown_future_key": 1,      # 前方互換: 黙って無視する
    }), encoding="utf-8")

    params = load_params(path)

    assert params.base_fare_usd == 999.0
    assert params.seasonality[10] == 2.0          # JSON の文字列キーが int に戻る
    assert params.airline_factors == DEFAULT_PARAMS.airline_factors   # 触っていない


def test_missing_params_file_falls_back_to_defaults(tmp_path):
    assert load_params(tmp_path / "does-not-exist.json") is DEFAULT_PARAMS
