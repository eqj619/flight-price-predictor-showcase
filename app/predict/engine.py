"""実測スナップショット -> 価格トレンドと買い時判定。

既存 flight-price-monitor の predict.py の後継。プロンプト 2.5 に挙がった
3つの欠陥と、Phase 0 で見つけた1つを構造的に潰してある。

| # | 欠陥（移植元） | 本実装 |
|---|---|---|
| 1 | 較正が直近1点のみ。外れ値1つで予測全体がズレる | 直近 N 点の比率の**中央値** |
| 2 | 「買い時」に過去日が出うる | 「過去最安の記録」と「これからの推奨」を別物にする |
| 3 | 予測が決定論的な1本の線。3日分と40日分が同じ確信度に見える | **必ず信頼帯を返す**。観測が少ないほど広い |
| 4 | simulated 行が実測として較正・推奨に混入する | 入力は `source='real'` のみ |

欠陥4は repository.history_for の既定引数で構造的に閉じてあるので、ここでは
既定のまま呼ぶ（`sources` を広げない）ことが実装上の約束になる。
"""
import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Optional, Sequence

from app import clock
from app.config import PREDICTION_LOG_HORIZON_DAYS, Settings, get_settings
from app.db import repository
from app.predict import simulator
from app.predict.curve import CurveParams, load_params, route_params
from app.predict.oil import OilAdapter

# 買い時判定の3値。
VERDICT_BUY_NOW = "buy_now"     # 今買う
VERDICT_WAIT = "wait"           # 待つ
VERDICT_WATCH = "watch"         # 様子見（帯が重なり区別できない）
# 3値の手前の状態。実測が1件も無く、モデル推定しか出せない。
VERDICT_NO_DATA = "insufficient_data"

# 予測の根拠。確かな順に3段階。UI はこれを見て表示を変える。混ぜて見せると
# PI-27-flightTrend と同じ「シミュレーション値を実測のように見せる
# ダッシュボード」になる。
#
#   observed    この出発日そのものの実測がある（通常の予測）
#   transferred 出発日の実測は無いが、同じルートの別の出発日の実測がある。
#               実際に観測された航空会社だけを、その便自身の過去実測で
#               較正して出す
#   simulated   このルートの実測が一切ない。曲線だけ。航空会社名は出さない
BASIS_OBSERVED = "observed"
# 往復。**実測は出すが、曲線由来のものは一切出さない。**
# 曲線は片道 212系列から推定したもので、往復では遠い側の平坦さしか
# 確かめられていない（docs/round-trip-findings-2026-08-19.md）。谷
# （50〜59日前 −4.2%）と遅延の罰（30日前以降 +16.7%）が往復でも成り立つかは
# 未測定。**判定の中身はその2つで決まる**ので、流用すれば課題2と同じ失敗になる。
BASIS_ROUND_TRIP_OBSERVED = "round_trip_observed"
# 往復で、まだ実測が1件も無い状態。**片道と違ってモデル推定に落とさない**ので、
# 「推定を出しています」と言ってはいけない。出していないのだから。
BASIS_ROUND_TRIP_PENDING = "round_trip_pending"
BASIS_TRANSFERRED = "transferred"
BASIS_SIMULATED = "simulated"
# その区間の実測を一度も見ていない状態。**路線があるかも分かっていない。**
# 推定を出すと「この路線はある」と読まれる（2026-08-30 の指摘）。
BASIS_UNCONFIRMED = "unconfirmed"
# 便が見つからない区間。**推定を出さないことを示す。**
# 運航されていない区間にモデル推定を出すと、存在しない便の値段を
# 断定的に見せることになる（2026-08-30 の報告: NRT-GMP）。
BASIS_NO_FLIGHTS = "no_flights"

# 出発30日前を過ぎたときの実測上昇率。2026-08-15、212系列 / 13,004点から。
# 35/41 系列で一致（符号検定 z=+4.5）。**曲線で唯一、強く支持された性質。**
# 画面で「遅らせるとどれだけ損か」を数字で言うために持つ。
LATE_BOOKING_PENALTY_PCT = 17

# 実測にもとづかない = 3値判定を出せない根拠。
_ESTIMATE_BASES = (BASIS_TRANSFERRED, BASIS_SIMULATED)


@dataclass(frozen=True)
class ObservedPoint:
    check_date: str
    price_usd: float


@dataclass(frozen=True)
class ProjectedPoint:
    check_date: str
    price_usd: float
    lower_usd: float
    upper_usd: float


@dataclass(frozen=True)
class ItineraryTrend:
    stops: int
    via_airport: Optional[str]
    airline: str
    # **便の身元の一部。** 同じ社・同じ経由地で1日に何本も飛ぶ。
    # '' は時刻の取れなかった便（列を足す前の実測など）。
    departure_time: str = ""
    observed: list[ObservedPoint] = field(default_factory=list)
    projected: list[ProjectedPoint] = field(default_factory=list)
    # 「実測◯日分」ラベルの値。信頼帯の広さもこれで決まる。
    observed_days: int = 0
    scale: float = 1.0
    dispersion: float = 0.0
    basis: str = BASIS_OBSERVED
    # transferred のときだけ埋まる。何件の・いつまでの実測を根拠にしたか。
    evidence_points: int = 0
    evidence_last_seen: Optional[str] = None
    # 総所要（飛行 + 乗継、分）。列を足す前の実測では None。
    total_minutes: Optional[int] = None

    def as_dict(self) -> dict:
        return asdict(self)


# ======================================================================
# 較正
# ======================================================================

def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def calibrate(rows: Sequence[dict], origin: str, destination: str,
              departure: date, stops: int, airline: str,
              params: CurveParams, window: int) -> tuple[float, float]:
    """(スケール, 相対ばらつき) を返す。

    **欠陥1の修正。** 移植元は直近1点だけで `scale = last_price / sim_at_last`
    としていた。最終観測日にたまたま特売やシステム不調の値が入ると、そこから
    出発日までの予測が丸ごとその倍率で歪む。直近 window 点の比率を取り、その
    **中央値**を使う。中央値なので、窓の半分未満の外れ値では動かない。

    ばらつき（比率の MAD をスケールで割ったもの）は信頼帯の幅に使う。
    実測が暴れている路線ほど帯が広くなるべきなので、ここで一緒に測る。
    """
    ratios = []
    for row in rows[-window:]:
        shape = simulator.price_shape(
            origin, destination, stops, airline,
            departure, date.fromisoformat(row["check_date"]), params,
        )
        if shape:
            ratios.append(row["price_usd"] / shape)

    if not ratios:
        return 1.0, 0.0

    scale = _median(ratios)
    if scale <= 0:
        return 1.0, 0.0

    mad = _median([abs(r - scale) for r in ratios])
    return scale, mad / scale


# ======================================================================
# 信頼帯
# ======================================================================

def band_ratio(dispersion: float, observed_days: int, days_ahead: int,
               horizon_days: int, settings: Settings) -> float:
    """予測値に対する信頼帯の相対半幅。

    **欠陥3の修正。** 移植元の予測は1本の決定論的な線で、実測3日分でも
    40日分でも同じ見た目だった。ここでは3つの要因で広げる。

    - 実測のばらつき（dispersion）が大きいほど広い
    - 観測日数が少ないほど広い（1/sqrt(n)）
    - 予測が先に伸びるほど広い

    観測が完全に一直線でも `band_base_uncertainty` の幅は残す。実測が
    きれいだったことは、未来もそうであることを意味しない。
    """
    small_sample = 1.0 + settings.band_small_sample_k / math.sqrt(
        max(observed_days, 1)
    )
    extrapolation = 1.0 + settings.band_horizon_k * (
        days_ahead / max(horizon_days, 1)
    )
    ratio = (settings.band_base_uncertainty + dispersion) * small_sample * extrapolation
    return min(ratio, settings.band_max_ratio)


# ======================================================================
# トレンド
# ======================================================================

def _latest_total_minutes(rows) -> Optional[int]:
    """その便の総所要。**最新の観測を採る。**

    同じ便でも日によってダイヤが変わることがあり、平均や最小を採ると
    実在しない所要時間になる。
    """
    for row in reversed(rows):
        if row.get("total_minutes") is not None:
            return row["total_minutes"]
    return None


def _within_duration(rows, max_total_minutes: Optional[int]):
    """総所要が長すぎる便を落とす。

    **total_minutes が NULL の行は残す。** 「不明」を「長すぎる」と同じに
    扱うと、この列を持つ前に集めた実測がまるごと消え、較正の入力が痩せる。
    列を足す前の行は遡って埋められない（区間の内訳を保存していないため）。
    """
    if not max_total_minutes or max_total_minutes <= 0:
        return rows
    return [r for r in rows
            if r["total_minutes"] is None or r["total_minutes"] <= max_total_minutes]


def build_trend(origin: str, destination: str, departure_date: str,
                airline_filter: Optional[Sequence[str]] = None,
                settings: Optional[Settings] = None,
                params: Optional[CurveParams] = None,
                oil: Optional[OilAdapter] = None,
                max_total_minutes: Optional[int] = None,
                return_date: str = "") -> dict[tuple, ItineraryTrend]:
    """便（経由回数・経由地・航空会社）ごとの実測列と予測列を返す。

    **往復（`return_date` あり）は予測列を返さない。** 曲線が往復で
    検証できていないため（BASIS_ROUND_TRIP_OBSERVED のコメント）。
    実測が無ければ何も返さない — 推定で埋めることもしない。
    """
    settings = settings or get_settings()
    # 方向別の上書き（curve.route_params）。区間が分かるここで当てる。
    params = route_params(origin, destination, params)
    oil = oil or OilAdapter(settings)

    # 既定の sources=('real',) のまま呼ぶ。**ここを広げてはいけない**
    # （欠陥4: simulated 行が較正と推奨に混入する）。
    rows = repository.history_for(origin, destination, departure_date,
                                  return_date=return_date)
    if airline_filter:
        allowed = set(airline_filter)
        rows = [r for r in rows if r["airline"] in allowed]
    rows = _within_duration(rows, max_total_minutes)
    if not rows and return_date:
        # **往復は推定で埋めない。** transferred も simulated も曲線に
        # 乗っているので、往復では根拠が無い。空で返し、画面は
        # 「まだ観測がありません」を出す。
        return {}
    if not rows:
        # この出発日の実測がまだ無い。確かな順に降りていく。
        #
        #   1. 同じルートの別の出発日に実測があれば、実際に観測された
        #      航空会社だけを、その便自身の過去実測で較正して出す
        #   2. それも無ければ曲線だけ（航空会社名は出さない）
        transferred = transferred_trends(origin, destination, departure_date,
                                         airline_filter, settings, params, oil,
                                         max_total_minutes=max_total_minutes)
        if transferred:
            return transferred

        if airline_filter:
            # しぼり込みの結果として何も残らなかった場合は、何も返さない。
            # ここで匿名の曲線を返すと、「そのルートを飛んでいない航空会社に
            # しぼった」のに価格が出てしまう（実測で NRT-SAN の最安は $708 な
            # のに、ZIPAIR でしぼると匿名推定の $448 が出た）。曲線は航空会社を
            # 区別しないので、しぼり込みの意図を表現できない。
            return {}

        return simulated_trends(origin, destination, departure_date,
                                settings, params, oil)

    departure = date.fromisoformat(departure_date)

    # **出発時刻まで見て分ける。** 同じ社・同じ経由地でも時刻が違えば
    # 別の便で、値段の水準も動き方も違う。3項目でまとめていた頃は
    # 「1本の線」に見えて中身が日ごとに入れ替わっていた（2026-09-08）。
    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["stops"], row["via_airport"], row["airline"],
               row["departure_time"] or "")
        grouped.setdefault(key, []).append(row)

    trends: dict[tuple, ItineraryTrend] = {}
    for (stops, via_airport, airline, departure_time), obs_rows in grouped.items():
        obs_rows.sort(key=lambda r: r["check_date"])
        scale, dispersion = calibrate(
            obs_rows, origin, destination, departure, stops, airline,
            params, settings.calibration_window,
        )

        last_check = date.fromisoformat(obs_rows[-1]["check_date"])
        horizon_days = max((departure - last_check).days, 1)
        observed_days = len(obs_rows)

        projected = []
        # **往復は予測線を引かない。** 曲線が往復で検証できていないので、
        # 引けば「根拠のある線」に見えてしまう。実測だけを見せる。
        cursor = departure + timedelta(days=1) if return_date \
            else last_check + timedelta(days=1)
        while cursor <= departure:
            shape = simulator.price_shape(
                origin, destination, stops, airline, departure, cursor, params,
            )
            if shape is not None:
                predicted = shape * scale * oil.adjustment_factor(cursor)
                half = predicted * band_ratio(
                    dispersion, observed_days, (cursor - last_check).days,
                    horizon_days, settings,
                )
                projected.append(ProjectedPoint(
                    check_date=cursor.isoformat(),
                    price_usd=round(predicted, 2),
                    lower_usd=round(max(predicted - half, 0.0), 2),
                    upper_usd=round(predicted + half, 2),
                ))
            cursor += timedelta(days=1)

        trends[(stops, via_airport, airline, departure_time)] = ItineraryTrend(
            stops=stops,
            via_airport=via_airport,
            airline=airline,
            departure_time=departure_time,
            observed=[ObservedPoint(r["check_date"], r["price_usd"])
                      for r in obs_rows],
            projected=projected,
            observed_days=observed_days,
            total_minutes=_latest_total_minutes(obs_rows),
            scale=round(scale, 4),
            dispersion=round(dispersion, 4),
            basis=BASIS_ROUND_TRIP_OBSERVED if return_date else BASIS_OBSERVED,
        )
    return trends


def transferred_trends(origin: str, destination: str, departure_date: str,
                       airline_filter: Optional[Sequence[str]] = None,
                       settings: Optional[Settings] = None,
                       params: Optional[CurveParams] = None,
                       oil: Optional[OilAdapter] = None,
                       today: Optional[date] = None,
                       max_total_minutes: Optional[int] = None) -> dict[tuple, ItineraryTrend]:
    """この出発日の実測は無いが、同じルートの別の出発日には実測がある場合。

    **実際に観測された航空会社だけ**を出す。曲線の航空会社係数表に頼って
    社名を並べるのではなく、「このルートで実際に返ってきた便」を根拠にする。
    そうしないと、その社が飛んでいない路線の運賃を出してしまう
    （ZIPAIR は NRT-SAN の通し運賃を売っていない）。

    価格は便ごとに自前で較正する。曲線の航空会社係数は実測の表記
    （"United", "ZIPAIR Tokyo"）と一致しないことが多く、係数に頼ると
    全社が同じ価格になって一覧の意味が無くなる。その便自身の過去実測から
    スケールを出せば、社ごとの水準差がそのまま出る。

    出発日をまたいだ転用が成り立つのは、出発日そのものの効果（季節・曜日・
    出発までの日数）を曲線側が既に織り込んでいるため。残るスケールは
    「その便がこのルートでどのくらいの水準か」なので持ち越せる。
    """
    settings = settings or get_settings()
    params = params or load_params()
    oil = oil or OilAdapter(settings)
    today = today or clock.today()

    departure = date.fromisoformat(departure_date)
    if departure < today:
        return {}

    since = (today - timedelta(days=settings.airline_evidence_days)).isoformat()
    rows = repository.route_history(origin, destination, since_check_date=since,
                                    exclude_departure_date=departure_date)
    if airline_filter:
        allowed = set(airline_filter)
        rows = [r for r in rows if r["airline"] in allowed]
    rows = _within_duration(rows, max_total_minutes)
    if not rows:
        return {}

    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        grouped.setdefault(
            (row["stops"], row["via_airport"], row["airline"],
             row["departure_time"] or ""), []).append(row)

    # 観測回数の多い便から採る。1回しか出ていない便を一覧の上位に並べない。
    ranked = sorted(grouped.items(), key=lambda kv: -len(kv[1]))
    horizon_days = max((departure - today).days, 1)

    trends: dict[tuple, ItineraryTrend] = {}
    for (stops, via_airport, airline, departure_time), obs in \
            ranked[:settings.top_n_airlines]:
        scale, dispersion = _transfer_scale(obs, origin, destination,
                                            stops, airline, params)
        if scale is None:
            continue

        projected = []
        cursor = today
        while cursor <= departure:
            shape = simulator.price_shape(origin, destination, stops, airline,
                                          departure, cursor, params)
            if shape is not None:
                predicted = shape * scale * oil.adjustment_factor(cursor)
                half = predicted * band_ratio(
                    dispersion + settings.band_transfer_dispersion, len(obs),
                    (cursor - today).days, horizon_days, settings,
                )
                projected.append(ProjectedPoint(
                    check_date=cursor.isoformat(),
                    price_usd=round(predicted, 2),
                    lower_usd=round(max(predicted - half, 0.0), 2),
                    upper_usd=round(predicted + half, 2),
                ))
            cursor += timedelta(days=1)

        if projected:
            trends[(stops, via_airport, airline, departure_time)] = ItineraryTrend(
                stops=stops, via_airport=via_airport, airline=airline,
                departure_time=departure_time,
                observed=[], projected=projected,
                observed_days=0,            # この出発日の実測は0日分
                scale=round(scale, 4), dispersion=round(dispersion, 4),
                basis=BASIS_TRANSFERRED,
                evidence_points=len(obs),
                evidence_last_seen=max(r["check_date"] for r in obs),
                total_minutes=_latest_total_minutes(
                    sorted(obs, key=lambda r: r["check_date"])),
            )
    return trends


def _transfer_scale(rows: Sequence[dict], origin: str, destination: str,
                    stops: int, airline: str,
                    params: CurveParams) -> tuple[Optional[float], float]:
    """別の出発日の実測から、その便のスケールと相対ばらつきを出す。

    各行を「その行自身の出発日」の曲線と比べる点が calibrate() と違う。
    """
    ratios = []
    for row in rows:
        shape = simulator.price_shape(
            origin, destination, stops, airline,
            date.fromisoformat(row["departure_date"]),
            date.fromisoformat(row["check_date"]), params,
        )
        if shape:
            ratios.append(row["price_usd"] / shape)

    if not ratios:
        return None, 0.0
    scale = _median(ratios)
    if scale <= 0:
        return None, 0.0
    return scale, _median([abs(r - scale) for r in ratios]) / scale


def simulated_trends(origin: str, destination: str, departure_date: str,
                     settings: Optional[Settings] = None,
                     params: Optional[CurveParams] = None,
                     oil: Optional[OilAdapter] = None,
                     today: Optional[date] = None) -> dict[tuple, ItineraryTrend]:
    """実測ゼロのときに、RM モデルだけで価格を出す。

    較正の材料が無いので、曲線の絶対水準（`base_fare_usd`）をそのまま使う。
    これは既存アプリの東京〜米西海岸市場の観測から置いた初期値なので、
    **水準は当てにならない。当てになるのは形（いつ底値になるか）のほう。**
    その不確かさを `band_no_data_dispersion` で帯の広さとして表に出す。

    **航空会社名は出さない。** 実測が無い状態で「ZIPAIR が $340」のように
    社名を付けると、その社がその路線を飛んでいなくても飛んでいるように
    見える（実測で ZIPAIR は NRT-SAN の通し運賃を売っていないことを確認済み）。
    モデルが言えるのは経由回数別の水準までで、それ以上は名乗らせない。
    """
    settings = settings or get_settings()
    params = params or load_params()
    oil = oil or OilAdapter(settings)
    today = today or clock.today()

    departure = date.fromisoformat(departure_date)
    if departure < today:
        return {}

    horizon_days = max((departure - today).days, 1)
    trends: dict[tuple, ItineraryTrend] = {}

    for stops in range(settings.max_stops + 1):
        projected = []
        cursor = today
        while cursor <= departure:
            # airline を空にすると default_airline_factor が効く。
            shape = simulator.price_shape(origin, destination, stops, "",
                                          departure, cursor, params)
            if shape is not None:
                predicted = shape * oil.adjustment_factor(cursor)
                half = predicted * band_ratio(
                    settings.band_no_data_dispersion, 1,
                    (cursor - today).days, horizon_days, settings,
                )
                projected.append(ProjectedPoint(
                    check_date=cursor.isoformat(),
                    price_usd=round(predicted, 2),
                    lower_usd=round(max(predicted - half, 0.0), 2),
                    upper_usd=round(predicted + half, 2),
                ))
            cursor += timedelta(days=1)

        if projected:
            trends[(stops, None, "")] = ItineraryTrend(
                stops=stops, via_airport=None, airline="",
                observed=[], projected=projected, observed_days=0,
                scale=1.0, dispersion=settings.band_no_data_dispersion,
                basis=BASIS_SIMULATED,
            )
    return trends


def expected_low_window(departure_date: str, today: Optional[date] = None,
                        params: Optional[CurveParams] = None) -> Optional[dict]:
    """モデルが底値と見る期間。

    **較正に依存しない**ので実測ゼロでも出せる（水準は較正で決まるが、
    どの日が相対的に安いかは proximity_factor の形だけで決まる）。

    **2026-08-15、実測から引き直した。** 取得元が応答に載せていた過去約61日分
    の価格履歴を全ルートに遡って取り込み（212系列 / 13,004点）、系列効果を
    除いて曲線を復元した。

    - 旧値の窓 45〜75日前は**広すぎた**。それ以前(76〜200日前)と差が出ない
      （32/66系列, 符号検定 z=-0.2）
    - **谷そのものは実在する。** 50〜59日前に絞ると -4.2%（29/40系列, z=+2.8）
    - ただし**遅延の罰のほうが4倍大きい**。出発30日前を過ぎると +16.7%
      （35/41系列, z=+4.5）

    **したがってこの窓は「ここが底」ではなく「ここより後は高くなる」の目印
    として扱う。** 数%の谷を狙って待たせると、外したときに17%を払わせる。
    「待っても安くならない」とまでは言えない（谷はある）ので、**言い切りにも
    しないこと**。詳細は docs/price-curve-2026-08-15.md。
    """
    params = params or load_params()
    today = today or clock.today()
    departure = date.fromisoformat(departure_date)

    start = departure - timedelta(days=params.sweet_spot_end_days)
    end = departure - timedelta(days=params.sweet_spot_start_days)
    # end は「曲線が平坦をやめて上がりはじめる日」でもある。画面が使うのは
    # そちらの意味なので、rises_after として別名で明示する（下の
    # price_rises_after を参照）。
    if end < today:
        return None     # 底値帯を過ぎている
    return {
        "start": max(start, today).isoformat(),
        "end": end.isoformat(),
        "days_before_departure": [params.sweet_spot_start_days,
                                  params.sweet_spot_end_days],
    }


def price_rises_after(departure_date: str, today: Optional[date] = None,
                      params: Optional[CurveParams] = None) -> Optional[dict]:
    """**この日を過ぎると価格が上がりはじめる。**

    2026-08-15 の実測（212系列 / 13,004点）で分かったのは、出発52日前より
    前がほぼ平坦で「待って安くなる時期」は無い、ということ。したがって
    利用者に伝えるべきは**待つ窓ではなく期限**になった。

    `expected_low_window` の end と同じ日付だが、**別の名前で返す。**
    画面が読むのは「期限」の意味であって「安い窓の終わり」ではない。
    同じ値を違う意味で使い回すと、曲線を引き直したときにどちらの意図で
    書かれたコードなのか判別できなくなる。

    - `date`  … 上がりはじめる日
    - `days`  … その日まであと何日（過ぎていれば負）
    - `penalty_pct` … 出発30日前を過ぎた場合の実測上昇率（35/41系列, z=+4.5）

    出発済みなら None。
    """
    params = params or load_params()
    today = today or clock.today()
    departure = date.fromisoformat(departure_date)
    if departure < today:
        return None

    rises = departure - timedelta(days=params.sweet_spot_start_days)
    return {
        "date": rises.isoformat(),
        "days": (rises - today).days,
        "days_before_departure": params.sweet_spot_start_days,
        "penalty_pct": LATE_BOOKING_PENALTY_PCT,
    }


# ======================================================================
# 買い時判定
# ======================================================================

@dataclass(frozen=True)
class Candidate:
    check_date: str
    price_usd: float
    stops: int
    via_airport: Optional[str]
    airline: str
    lower_usd: Optional[float] = None
    upper_usd: Optional[float] = None


def recommend(origin: str, destination: str, departure_date: str,
              today: Optional[date] = None,
              airline_filter: Optional[Sequence[str]] = None,
              settings: Optional[Settings] = None,
              params: Optional[CurveParams] = None,
              oil: Optional[OilAdapter] = None,
              max_total_minutes: Optional[int] = None,
              return_date: str = "") -> Optional[dict]:
    """買い時の判定。

    **往復には判定を出さない。** 実測（いま買える最安・過去最安）は返すが、
    `verdict` / `predicted_min` / `expected_low_window` / `price_rises_after`
    はすべて None にする。これらは曲線から出るもので、曲線は往復で
    検証できていない（BASIS_ROUND_TRIP_OBSERVED のコメント）。

    **欠陥2の修正。** 移植元は実測と予測を混ぜた列の最小値を「推奨購入日」と
    して返していたため、最安が過去にあると「2週間前に買え」という実行不能な
    推奨が出た。ここでは3つを別のフィールドに分ける。

    - `lowest_observed` — 過去最安の**記録**（買えない。判断材料としての情報）
    - `current_best`    — 直近の実測での最安（今買える価格）
    - `predicted_min`   — これから先の予測最小値（未来日のみ）

    判定は**信頼帯どうしの重なりで決める**。PI-27-flightTrend のように
    「7日以内なら confidence 85%」といった根拠のない固定値は使わない。
    """
    settings = settings or get_settings()
    today = today or clock.today()
    # 方向別の上書き（curve.route_params）。期限・底値帯もこの区間の値で出す。
    params = route_params(origin, destination, params)

    trends = build_trend(origin, destination, departure_date,
                         airline_filter, settings, params, oil,
                         max_total_minutes=max_total_minutes,
                         return_date=return_date)
    if not trends:
        return None

    if all(t.basis in _ESTIMATE_BASES for t in trends.values()):
        # この出発日の実売価格が1件も無い。比べる相手がいないので3値判定は
        # 出さない（社名が出る transferred でも同じ）。
        return _model_only_recommendation(origin, destination, departure_date,
                                          trends, today, params)

    lowest_observed = None
    current_best = None
    latest_check = max(t.observed[-1].check_date for t in trends.values()
                       if t.observed)

    for trend in trends.values():
        for point in trend.observed:
            candidate = Candidate(point.check_date, point.price_usd,
                                  trend.stops, trend.via_airport, trend.airline)
            if lowest_observed is None or point.price_usd < lowest_observed.price_usd:
                lowest_observed = candidate
            if point.check_date == latest_check and (
                current_best is None or point.price_usd < current_best.price_usd
            ):
                current_best = candidate

    # これから先の予測のみ。過去日は推奨に載せない。
    #
    # **今日も含めない（2026-10-17）。** 「待つ」は明日以降に買うこと。今日の
    # 予測点を候補に入れると、出発当日でも「今日のモデル上の価格 < 今日の実売」
    # だけで「待つ」が出ていた（10/12〜14 出発の判定で実際に出た。待てる日が
    # もう無いのに）。今日より先が無ければ predicted_min は None になり、
    # _decide が「今買う」を返す。
    predicted_min = None
    for trend in trends.values():
        for point in trend.projected:
            if date.fromisoformat(point.check_date) <= today:
                continue
            if predicted_min is None or point.price_usd < predicted_min.price_usd:
                predicted_min = Candidate(
                    point.check_date, point.price_usd, trend.stops,
                    trend.via_airport, trend.airline,
                    lower_usd=point.lower_usd, upper_usd=point.upper_usd,
                )

    observed_days = max(t.observed_days for t in trends.values())

    if return_date:
        # 実測だけ。曲線から出るものは全部落とす。
        return {
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "return_date": return_date,
            "as_of": today.isoformat(),
            "basis": BASIS_ROUND_TRIP_OBSERVED,
            "observed_days": observed_days,
            "latest_check_date": latest_check,
            "verdict": None,
            "recommended_check_date": None,
            "current_best": asdict(current_best) if current_best else None,
            "lowest_observed": asdict(lowest_observed) if lowest_observed else None,
            "predicted_min": None,
            "expected_low_window": None,
            "price_rises_after": None,
        }

    verdict, recommended_date = _decide(current_best, predicted_min, today)

    return {
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "return_date": "",
        "as_of": today.isoformat(),
        "basis": BASIS_OBSERVED,
        "observed_days": observed_days,      # 「実測◯日分」ラベル
        "latest_check_date": latest_check,
        "verdict": verdict,
        "recommended_check_date": recommended_date,
        "current_best": asdict(current_best) if current_best else None,
        "lowest_observed": asdict(lowest_observed) if lowest_observed else None,
        "predicted_min": asdict(predicted_min) if predicted_min else None,
        "expected_low_window": expected_low_window(departure_date, today, params),
        "price_rises_after": price_rises_after(departure_date, today, params),
    }


def _model_only_recommendation(origin: str, destination: str,
                               departure_date: str,
                               trends: dict[tuple, ItineraryTrend],
                               today: date,
                               params: Optional[CurveParams]) -> dict:
    """実測ゼロのときの返り値。

    **3値判定は出さない。** 3値は「今の実売価格」と「予測」を比べて決めるもので、
    実測が1件も無ければ比べる相手がいない。代わりに、較正なしでも言える
    「いつが底値帯か」を返す。
    """
    predicted_min = None
    for trend in trends.values():
        for point in trend.projected:
            if date.fromisoformat(point.check_date) <= today:
                continue
            if predicted_min is None or point.price_usd < predicted_min.price_usd:
                predicted_min = Candidate(
                    point.check_date, point.price_usd, trend.stops,
                    trend.via_airport, trend.airline,
                    lower_usd=point.lower_usd, upper_usd=point.upper_usd,
                )

    sample = next(iter(trends.values()))
    return {
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "as_of": today.isoformat(),
        "basis": sample.basis,
        "observed_days": 0,
        "evidence_points": max(t.evidence_points for t in trends.values()),
        "evidence_last_seen": max(
            (t.evidence_last_seen for t in trends.values() if t.evidence_last_seen),
            default=None),
        "latest_check_date": None,
        "verdict": VERDICT_NO_DATA,
        "recommended_check_date": None,
        "current_best": None,
        "lowest_observed": None,
        "predicted_min": asdict(predicted_min) if predicted_min else None,
        "expected_low_window": expected_low_window(departure_date, today, params),
        "price_rises_after": price_rises_after(departure_date, today, params),
    }


def _decide(current_best: Optional[Candidate], predicted_min: Optional[Candidate],
            today: date) -> tuple[str, Optional[str]]:
    """3値判定。「待って得をする見込み」がどれだけ確かかで決める。

    - 予測の**上限**が今の価格未満 = 悪く転んでも今より安い -> 待つ
    - 予測の**点推定**が今の価格未満（上限は今の価格以上）  -> 様子見
    - 予測の**点推定**が今の価格以上 = 期待値として得がない -> 今買う

    信頼帯が効くのは「待つ」と「様子見」の境目。実測が少なく帯が広いうちは
    上限が今の価格を超えるので「待つ」と言い切らず「様子見」に倒れる。日数が
    増えて帯が狭まると初めて「待つ」が立つ。**確信度を数値で捏造しない**
    （PI-27-flightTrend の「7日以内なら 85%」のような固定値は使わない）。
    帯の広さと「実測◯日分」ラベルが、そのまま根拠として表に出る。

    下限で「今買う」を判定しない理由: 下限が今の価格を超えるには、1日分の
    価格上昇が帯の幅を上回る必要がある。曲線の傾きは直前期でも日次3〜4%で、
    帯の最低幅 5% を超えられない。下限判定にすると「今買う」が原理的にほぼ
    出ず、実質2値になってしまう。
    """
    if predicted_min is None:
        # 予測できる未来がない（出発日を過ぎている等）。
        return VERDICT_BUY_NOW, today.isoformat() if current_best else None
    if current_best is None:
        return VERDICT_WATCH, None

    upper = predicted_min.upper_usd
    if upper is not None and upper < current_best.price_usd:
        return VERDICT_WAIT, predicted_min.check_date
    if predicted_min.price_usd < current_best.price_usd:
        return VERDICT_WATCH, None
    return VERDICT_BUY_NOW, today.isoformat()


# ======================================================================
# prediction_log（精度検証用）
# ======================================================================

def log_predictions(origin: str, destination: str, departure_date: str,
                    predicted_on: Optional[date] = None,
                    settings: Optional[Settings] = None,
                    params: Optional[CurveParams] = None,
                    oil: Optional[OilAdapter] = None) -> int:
    """後から答え合わせできるよう、予測を prediction_log に残す。

    保存するのは**そのルートで最安の便**について、決まった日数先
    （PREDICTION_LOG_HORIZON_DAYS）の予測だけ。全便 x 全日を残すと
    500ルート規模で日次数十万行になる一方、検証したいのは「何日先まで
    当たるか」なので地平線を絞れば足りる。
    """
    settings = settings or get_settings()
    predicted_on = predicted_on or clock.today()

    trends = build_trend(origin, destination, departure_date,
                         None, settings, params, oil)
    # モデル推定だけの予測は記録しない。prediction_log は「較正済みの予測が
    # どれだけ当たったか」を検証するための表なので、較正していない値を
    # 混ぜると検証にならない。
    trends = {k: t for k, t in trends.items() if t.basis == BASIS_OBSERVED}
    if not trends:
        return 0

    cheapest = min(
        (t for t in trends.values() if t.projected),
        key=lambda t: min(p.price_usd for p in t.projected),
        default=None,
    )
    if cheapest is None:
        return 0

    by_date = {p.check_date: p for p in cheapest.projected}
    predictions = []
    for horizon in PREDICTION_LOG_HORIZON_DAYS:
        target = (predicted_on + timedelta(days=horizon)).isoformat()
        point = by_date.get(target)
        if point is None:
            continue    # 出発日を越えた地平線は残さない
        predictions.append(repository.Prediction(
            predicted_on=predicted_on.isoformat(),
            target_check_date=target,
            departure_date=departure_date,
            origin=origin,
            destination=destination,
            stops=cheapest.stops,
            via_airport=cheapest.via_airport,
            airline=cheapest.airline,
            # **便まで一致させないと答え合わせが別の便を拾う。**
            departure_time=cheapest.departure_time,
            predicted_usd=point.price_usd,
            lower_usd=point.lower_usd,
            upper_usd=point.upper_usd,
            observed_points=cheapest.observed_days,
        ))

    return repository.insert_predictions(predictions)
