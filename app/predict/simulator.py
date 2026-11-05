"""決定論的な価格シミュレータ。

既存 flight-price-monitor の simulator.py の移植。定数は curve.py に
外出ししてある。

「決定論的」とは、同じ入力からは常に同じ出力が出るという意味。ゆらぎは
入力のハッシュから種を作るので、今日走らせたバックフィルと来週走らせた
バックフィルが同じ値になる。システム時刻や os.urandom は使わない。

**価格曲線と最終価格を別の関数に分けてある点が移植元との違い。**

- `price_shape()` — なめらかな曲線そのもの。ゆらぎも丸めも無い
- `simulate_price()` — shape x ゆらぎ、5ドル単位に丸め

較正と予測には `price_shape()` を使う。移植元は較正にも `simulate_price()`
を使っていたため、較正点に乗った +3% のゆらぎが scale に焼き込まれ、
その先の予測日には別のゆらぎが掛かっていた。実在しない ±3% のギザギザを
予測線に載せることになるので分離した。`simulate_price()` は
`source='simulated'` のスナップショットを作る用途に残してある。
"""
import hashlib
import random
from datetime import date
from typing import Optional

from app.predict.curve import DEFAULT_PARAMS, CurveParams


def list_airlines(params: Optional[CurveParams] = None) -> list[str]:
    return list((params or DEFAULT_PARAMS).airline_factors.keys())


def _stops_factor(stops: int, params: CurveParams) -> float:
    # 実測は Google の通し運賃を取るので、経由地は説明用のメタデータに
    # すぎず、ここでのモデル価格は変えない。
    return params.nonstop_factor if stops == 0 else params.connecting_factor


def _seasonality_factor(departure_date: date, params: CurveParams) -> float:
    return params.seasonality.get(departure_date.month, 1.0)


def _day_of_week_factor(departure_date: date, params: CurveParams) -> float:
    return params.day_of_week.get(departure_date.weekday(), 1.0)


def proximity_factor(days_until_departure: int,
                     params: Optional[CurveParams] = None) -> float:
    """出発までの日数に対する価格倍率。

    較正後の予測は実質この関数の形だけで決まる（他の係数はすべて
    check_date に依存しないのでスケール項に吸収される）。Phase 3.5 で
    実測から推定するのはここのパラメータ。
    """
    p = params or DEFAULT_PARAMS
    days = max(days_until_departure, 0)

    if p.sweet_spot_start_days <= days <= p.sweet_spot_end_days:
        # 座席を埋めるために各社が下げる底値帯
        return p.sweet_spot_factor

    if days > p.sweet_spot_end_days:
        # 底値帯より先。ゆるやかに戻る（まだ高くはない）
        extra = min((days - p.sweet_spot_end_days) / p.far_taper_days, 1.0)
        return p.sweet_spot_factor + extra * p.far_taper_amount

    if days > p.ramp_end_days:
        # 底値帯から直線的に上昇
        span = p.sweet_spot_start_days - p.ramp_end_days
        progressed = (days - p.ramp_end_days) / span
        return p.ramp_top_factor - progressed * (p.ramp_top_factor - p.sweet_spot_factor)

    # 直前需要の跳ね上がり
    ramp = (p.ramp_end_days - days) / p.ramp_end_days
    return p.last_minute_start_factor + ramp * (
        p.last_minute_peak_factor - p.last_minute_start_factor
    )


def price_shape(origin: str, destination: str, stops: int, airline: str,
                departure_date: date, check_date: date,
                params: Optional[CurveParams] = None) -> Optional[float]:
    """なめらかな価格曲線（ゆらぎ・丸めなし）。

    check_date が出発日を過ぎていれば None（値付けする対象がない）。
    """
    p = params or DEFAULT_PARAMS
    days_until_departure = (departure_date - check_date).days
    if days_until_departure < 0:
        return None

    return (
        p.base_fare_usd
        * _stops_factor(stops, p)
        * p.airline_factors.get(airline, p.default_airline_factor)
        * _seasonality_factor(departure_date, p)
        * _day_of_week_factor(departure_date, p)
        * proximity_factor(days_until_departure, p)
    )


def _seeded_jitter(params: CurveParams, *parts: str) -> float:
    key = "|".join(parts)
    digest = hashlib.sha256(key.encode()).hexdigest()
    seed = int(digest[:16], 16)
    return random.Random(seed).uniform(params.jitter_min, params.jitter_max)


def simulate_price(origin: str, destination: str, stops: int, airline: str,
                   departure_date: date, check_date: date,
                   via_airport: Optional[str] = None,
                   params: Optional[CurveParams] = None) -> Optional[float]:
    """実運賃らしく見える一方向運賃（USD）。

    `source='simulated'` のスナップショットを作る用途。**較正と予測には
    使わないこと**（`price_shape()` を使う）。
    """
    p = params or DEFAULT_PARAMS
    shape = price_shape(origin, destination, stops, airline,
                        departure_date, check_date, p)
    if shape is None:
        return None

    shape *= _seeded_jitter(
        p, origin, destination, str(stops), via_airport or "", airline,
        departure_date.isoformat(), check_date.isoformat(),
    )
    return round(shape / 5) * 5
