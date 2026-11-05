"""予測エンジンのテスト。

プロンプト 2.5 に挙がった3つの欠陥 + Phase 0 で見つけた1つが、実装として
閉じていることを固定する。移植元 flight-price-monitor の predict.py は
どれも踏んでいた。
"""
from datetime import date, timedelta

import pytest

from app.config import get_settings
from app.db import repository
from app.db.connection import get_connection
from app.predict import simulator
from app.predict.engine import (
    VERDICT_BUY_NOW,
    VERDICT_NO_DATA,
    VERDICT_WAIT,
    VERDICT_WATCH,
    Candidate,
    _decide,
    band_ratio,
    build_trend,
    log_predictions,
    recommend,
)

TODAY = date(2026, 8, 8)
DEPARTURE = "2026-10-15"        # TODAY の68日後


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """**アプリの「今日」を TODAY に固定する。**

    recommend(today=TODAY) と渡しても、build_trend の先（他の出発日からの
    推定・モデルだけの推定）は clock.today() を読む。実際の日付が DEPARTURE
    （2026-10-15）を過ぎた 2026-10-17 に、この11件が一斉に落ちた。
    """
    monkeypatch.setattr("app.clock.today", lambda: TODAY)
ORIGIN, DESTINATION = "NRT", "LAX"


def _seed(prices: dict[date, float], airline="ANA", stops=0, via=None,
          source="real", origin=ORIGIN, destination=DESTINATION,
          departure=DEPARTURE):
    repository.upsert_snapshots([
        repository.Snapshot(
            check_date=check_date.isoformat(),
            departure_date=departure,
            origin=origin,
            destination=destination,
            stops=stops,
            via_airport=via,
            airline=airline,
            price_usd=price,
            source=source,
            fetched_at=check_date.isoformat(),
        )
        for check_date, price in prices.items()
    ])


def _on_curve(days_back: range, factor: float = 1.0, airline="ANA",
              departure=DEPARTURE) -> dict[date, float]:
    """曲線にぴったり乗る実測列を作る（比率がすべて factor になる）。"""
    dep = date.fromisoformat(departure)
    out = {}
    for d in days_back:
        check_date = TODAY - timedelta(days=d)
        shape = simulator.price_shape(ORIGIN, DESTINATION, 0, airline,
                                      dep, check_date)
        out[check_date] = round(shape * factor, 2)
    return out


def _trend():
    return build_trend(ORIGIN, DESTINATION, DEPARTURE)


# ======================================================================
# 欠陥1 — 較正が直近1点のみ
# ======================================================================

def test_a_single_outlier_on_the_last_day_does_not_move_the_calibration():
    _seed(_on_curve(range(9, -1, -1)))
    clean_scale = next(iter(_trend().values())).scale

    # 最終観測日に4倍の外れ値。移植元は scale = last_price / sim_at_last
    # だったので、この1点で出発日までの予測が丸ごと4倍に歪んだ。
    _seed({TODAY: _on_curve(range(0, 1))[TODAY] * 4})
    dirty_scale = next(iter(_trend().values())).scale

    assert abs(dirty_scale - clean_scale) / clean_scale < 0.05


def test_calibration_still_tracks_a_genuine_level_shift():
    # 中央値なので、外れ値には動かないが「本当に全体が上がった」ときは追う。
    _seed(_on_curve(range(9, -1, -1), factor=1.0))
    before = next(iter(_trend().values())).scale

    _seed(_on_curve(range(9, -1, -1), factor=1.5))
    after = next(iter(_trend().values())).scale

    assert after > before * 1.4


def test_calibration_only_uses_the_recent_window():
    window = get_settings().calibration_window
    # 古い倍の値と、直近 window 日分の等倍。窓の外は無視されるべき。
    _seed(_on_curve(range(window + 10, window, -1), factor=2.0))
    _seed(_on_curve(range(window - 1, -1, -1), factor=1.0))

    assert next(iter(_trend().values())).scale == pytest.approx(1.0, abs=0.05)


# ======================================================================
# 欠陥2 — 「買い時」に過去日が出る
# ======================================================================

def test_the_recommended_date_is_never_in_the_past():
    # 過去に特売があり、それが全期間の最安という状況。移植元は
    # observed + projected の最小値を返していたので、この特売日を
    # 「推奨購入日」として返した（実行不能な推奨）。
    prices = _on_curve(range(9, -1, -1))
    oldest = min(prices)
    prices[oldest] = 50.0
    _seed(prices)

    result = recommend(ORIGIN, DESTINATION, DEPARTURE, today=TODAY)

    assert result["recommended_check_date"] is None or \
        date.fromisoformat(result["recommended_check_date"]) >= TODAY


def test_the_past_low_is_reported_as_a_record_not_as_a_recommendation():
    prices = _on_curve(range(9, -1, -1))
    oldest = min(prices)
    prices[oldest] = 50.0
    _seed(prices)

    result = recommend(ORIGIN, DESTINATION, DEPARTURE, today=TODAY)

    # 過去最安は「情報」として別フィールドに出る。
    assert result["lowest_observed"]["price_usd"] == 50.0
    assert result["lowest_observed"]["check_date"] == oldest.isoformat()
    # 推奨には混ざらない。
    assert result["predicted_min"]["price_usd"] != 50.0


def test_current_best_comes_from_the_latest_check_date():
    _seed(_on_curve(range(9, -1, -1)))
    result = recommend(ORIGIN, DESTINATION, DEPARTURE, today=TODAY)
    assert result["current_best"]["check_date"] == TODAY.isoformat()


def test_all_projected_dates_are_after_the_last_observation():
    _seed(_on_curve(range(4, -1, -1)))
    for trend in _trend().values():
        assert all(p.check_date > TODAY.isoformat() for p in trend.projected)


# ======================================================================
# 欠陥3 — 信頼帯がない
# ======================================================================

def test_every_projected_point_carries_a_band():
    _seed(_on_curve(range(9, -1, -1)))
    for trend in _trend().values():
        assert trend.projected
        for point in trend.projected:
            assert point.lower_usd < point.price_usd < point.upper_usd


def test_fewer_observations_give_a_wider_band():
    settings = get_settings()
    wide = band_ratio(0.0, observed_days=3, days_ahead=10,
                      horizon_days=60, settings=settings)
    narrow = band_ratio(0.0, observed_days=40, days_ahead=10,
                        horizon_days=60, settings=settings)
    # 実測3日分と40日分が同じ確信度に見えてはいけない。
    assert wide > narrow


def test_a_longer_horizon_gives_a_wider_band():
    settings = get_settings()
    near = band_ratio(0.0, 10, days_ahead=1, horizon_days=60, settings=settings)
    far = band_ratio(0.0, 10, days_ahead=60, horizon_days=60, settings=settings)
    assert far > near


def test_noisier_observations_give_a_wider_band():
    settings = get_settings()
    calm = band_ratio(0.0, 10, 10, 60, settings)
    noisy = band_ratio(0.4, 10, 10, 60, settings)
    assert noisy > calm


def test_the_band_never_collapses_even_with_perfect_observations():
    settings = get_settings()
    # 実測がきれいだったことは、未来もそうであることを意味しない。
    assert band_ratio(0.0, 10_000, 1, 60, settings) > 0


def test_the_band_is_capped():
    settings = get_settings()
    assert band_ratio(99.0, 1, 60, 60, settings) == settings.band_max_ratio


def test_a_short_series_really_produces_a_wider_band_end_to_end():
    _seed(_on_curve(range(2, -1, -1)))                      # 実測3日分
    _seed(_on_curve(range(29, -1, -1)), destination="SFO")  # 実測30日分

    def width(destination):
        trends = build_trend(ORIGIN, destination, DEPARTURE)
        point = next(iter(trends.values())).projected[0]
        return (point.upper_usd - point.lower_usd) / point.price_usd

    assert width("LAX") > width("SFO")


def test_observed_days_label_counts_distinct_check_dates():
    _seed(_on_curve(range(6, -1, -1)))
    result = recommend(ORIGIN, DESTINATION, DEPARTURE, today=TODAY)
    assert result["observed_days"] == 7


# ======================================================================
# 欠陥4 — simulated 行の混入
# ======================================================================

def test_simulated_rows_do_not_reach_the_calibration_or_the_recommendation():
    _seed(_on_curve(range(9, -1, -1)))
    # バックフィルしたシミュレーション値が極端に安いケース。移植元は
    # history_for が source を区別せず、これを実測として掴んで
    # 「最安」として推奨しえた。
    _seed({TODAY - timedelta(days=3): 1.0}, airline="ANA", source="simulated",
          stops=1, via="ICN")

    result = recommend(ORIGIN, DESTINATION, DEPARTURE, today=TODAY)

    assert result["lowest_observed"]["price_usd"] > 1.0
    keys = build_trend(ORIGIN, DESTINATION, DEPARTURE).keys()
    assert all(stops == 0 for stops, _via, _airline, _dep in keys)


def test_simulated_rows_are_stored_but_simply_not_read():
    _seed(_on_curve(range(2, -1, -1)))
    _seed({TODAY: 1.0}, airline="ZIPAIR", source="simulated")

    with get_connection() as conn:
        stored = conn.execute(
            "SELECT COUNT(*) c FROM snapshots WHERE source = 'simulated'"
        ).fetchone()["c"]

    # 行は残る（グラフの補助線などで明示的に読む余地を残す）。
    assert stored == 1
    # ただし既定の読み出しには入らない。
    assert all(t.airline != "ZIPAIR" for t in _trend().values())


# ======================================================================
# 3値判定
# ======================================================================

def test_buy_now_when_even_the_optimistic_future_is_not_cheaper():
    current = Candidate("2026-08-08", 500.0, 0, None, "ANA")
    future = Candidate("2026-09-01", 700.0, 0, None, "ANA",
                       lower_usd=650.0, upper_usd=750.0)
    verdict, recommended = _decide(current, future, TODAY)
    assert verdict == VERDICT_BUY_NOW
    assert recommended == TODAY.isoformat()


def test_wait_when_even_the_pessimistic_future_is_cheaper():
    current = Candidate("2026-08-08", 900.0, 0, None, "ANA")
    future = Candidate("2026-09-01", 600.0, 0, None, "ANA",
                       lower_usd=550.0, upper_usd=650.0)
    verdict, recommended = _decide(current, future, TODAY)
    assert verdict == VERDICT_WAIT
    assert recommended == "2026-09-01"


def test_watch_when_the_bands_overlap():
    current = Candidate("2026-08-08", 700.0, 0, None, "ANA")
    future = Candidate("2026-09-01", 680.0, 0, None, "ANA",
                       lower_usd=600.0, upper_usd=760.0)
    verdict, recommended = _decide(current, future, TODAY)
    # 区別できないときに数値の確信度を捏造しない。日数が増えて帯が
    # 狭まれば自然に buy_now / wait へ倒れる。
    assert verdict == VERDICT_WATCH
    assert recommended is None


def test_the_band_width_alone_can_flip_wait_into_watch():
    """実測日数の違いが、そのまま判定の慎重さになることを固定する。

    同じ「未来は 8% 安い」という予測でも、帯が広ければ「待つ」と言い切らず
    「様子見」に留まる。判定を分けているのが帯の幅だけであることを示す。
    """
    settings = get_settings()
    current = Candidate("2026-08-08", 1000.0, 0, None, "ANA")

    def verdict_with(observed_days: int) -> str:
        predicted = 920.0
        half = predicted * band_ratio(0.0, observed_days, days_ahead=30,
                                      horizon_days=60, settings=settings)
        future = Candidate("2026-09-07", predicted, 0, None, "ANA",
                           lower_usd=predicted - half, upper_usd=predicted + half)
        return _decide(current, future, TODAY)[0]

    assert verdict_with(3) == VERDICT_WATCH
    assert verdict_with(60) == VERDICT_WAIT


def test_a_flat_sweet_spot_says_buy_now():
    # 出発68日前は底値帯のただ中で、モデル上これ以上は下がらない。
    # 待っても期待値として得がないなら「今買う」。
    _seed(_on_curve(range(9, -1, -1)))
    result = recommend(ORIGIN, DESTINATION, DEPARTURE, today=TODAY)
    assert result["verdict"] == VERDICT_BUY_NOW
    assert result["recommended_check_date"] == TODAY.isoformat()


def test_far_from_departure_does_not_tell_you_to_wait():
    """**出発200日前でも「待て」とは言わない。**

    2026-08-15 に曲線を実測から引き直すまでは、ここは「底値帯(45〜75日前)まで
    待て」と答えていた。実測ではその帯とそれ以前(76〜200日前)に差が無く
    （32/66系列, 符号検定 z=-0.2）、**待って得るものが無い。** 一方で
    出発30日前を過ぎると +16.7%（35/41系列, z=+4.5）の罰がある。

    待たせる助言は、利得ゼロと引き換えに取り逃しの危険だけを負わせる。
    """
    far = (TODAY + timedelta(days=200)).isoformat()
    _seed(_on_curve(range(29, -1, -1), departure=far), departure=far)

    result = recommend(ORIGIN, DESTINATION, far, today=TODAY)

    assert result["verdict"] != VERDICT_WAIT


# ======================================================================
# その他
# ======================================================================

# ======================================================================
# 実測ゼロのときのモデル推定
# ======================================================================

def test_no_history_falls_back_to_the_model():
    result = recommend(ORIGIN, DESTINATION, DEPARTURE, today=TODAY)

    assert result["basis"] == "simulated"
    assert result["predicted_min"]["price_usd"] > 0
    # 3値判定は「今の実売価格」と比べて決めるもの。比べる相手がいない。
    assert result["verdict"] == VERDICT_NO_DATA
    assert result["current_best"] is None
    assert result["recommended_check_date"] is None


def test_the_model_only_band_is_wider_than_a_calibrated_one():
    model_only = build_trend(ORIGIN, DESTINATION, DEPARTURE)
    model_point = next(iter(model_only.values())).projected[0]
    model_width = (model_point.upper_usd - model_point.lower_usd) / model_point.price_usd

    _seed(_on_curve(range(9, -1, -1)))
    calibrated = build_trend(ORIGIN, DESTINATION, DEPARTURE)
    real_point = next(iter(calibrated.values())).projected[0]
    real_width = (real_point.upper_usd - real_point.lower_usd) / real_point.price_usd

    # 較正していない推定のほうが不確か。それが帯の広さに出る。
    assert model_width > real_width


def test_one_real_price_replaces_the_model_estimate():
    _seed(_on_curve(range(0, 1)))
    trends = build_trend(ORIGIN, DESTINATION, DEPARTURE)
    assert all(t.basis == "observed" for t in trends.values())


def test_model_estimates_never_reach_the_prediction_log():
    # prediction_log は「較正済みの予測が当たったか」を検証する表。
    # 較正していない値を混ぜると検証にならない。
    assert log_predictions(ORIGIN, DESTINATION, DEPARTURE, predicted_on=TODAY) == 0


def test_the_expected_low_window_follows_the_curve_parameters():
    """帯は曲線パラメータそのもの。**日数を直書きしない** — 曲線は実測から
    推定し直す対象で、特定の値はこのテストの主張ではない。"""
    from app.predict.curve import route_params

    # 方向で期限が違う（2026-10-14）。この区間の値と比べる。
    params = route_params(ORIGIN, DESTINATION)
    result = recommend(ORIGIN, DESTINATION, DEPARTURE, today=TODAY)
    window = result["expected_low_window"]

    departure = date.fromisoformat(DEPARTURE)
    assert (departure - date.fromisoformat(window["end"])).days == \
        params.sweet_spot_start_days
    # 帯の始まりは today で切られることがある（過去は出さない）。
    assert (departure - date.fromisoformat(window["start"])).days <= \
        params.sweet_spot_end_days
    assert date.fromisoformat(window["start"]) >= TODAY


def test_there_is_no_low_window_once_it_has_passed():
    from app.predict.engine import expected_low_window

    near = (TODAY + timedelta(days=10)).isoformat()
    assert expected_low_window(near, TODAY) is None


def test_a_departed_route_produces_nothing():
    past = (TODAY - timedelta(days=1)).isoformat()
    assert recommend(ORIGIN, DESTINATION, past, today=TODAY) is None


# ======================================================================
# 別の出発日の実測からの転用（transferred）
# ======================================================================

OTHER_DEPARTURE = "2026-12-20"


def _seed_other_departure(price=900.0, airline="ANA", days=range(4, -1, -1),
                          stops=0, via=None):
    """同じルートの「別の出発日」の実測を仕込む。"""
    _seed({TODAY - timedelta(days=d): price for d in days},
          airline=airline, stops=stops, via=via, departure=OTHER_DEPARTURE)


def test_another_departure_date_supplies_the_airline_names():
    _seed_other_departure(airline="ZIPAIR Tokyo")

    trends = build_trend(ORIGIN, DESTINATION, DEPARTURE)

    assert all(t.basis == "transferred" for t in trends.values())
    # 曲線の係数表ではなく、このルートで実際に返ってきた社名を出す。
    assert {t.airline for t in trends.values()} == {"ZIPAIR Tokyo"}


def test_only_airlines_actually_seen_on_this_route_are_named():
    _seed_other_departure(airline="ANA")

    airlines = {t.airline for t in build_trend(ORIGIN, DESTINATION, DEPARTURE).values()}

    # curve.py の係数表には JAL も United もあるが、このルートで観測されて
    # いないので出さない。
    assert airlines == {"ANA"}


def test_an_unseen_route_still_falls_back_to_anonymous_estimates():
    _seed_other_departure(airline="ANA")

    # 別ルート（LAX 行き）には実測が無いので社名を出さない。
    trends = build_trend(ORIGIN, "SFO", DEPARTURE)

    assert all(t.basis == "simulated" for t in trends.values())
    assert {t.airline for t in trends.values()} == {""}


def test_transferred_prices_differ_per_airline():
    _seed_other_departure(price=2200.0, airline="ANA")
    _seed_other_departure(price=500.0, airline="ZIPAIR Tokyo")

    trends = build_trend(ORIGIN, DESTINATION, DEPARTURE)
    first = {t.airline: t.projected[0].price_usd for t in trends.values()}

    # 便ごとに自前で較正するので、社ごとの水準差がそのまま出る。
    # 曲線の航空会社係数に頼ると（実測の表記と一致せず）全社同額になる。
    assert first["ZIPAIR Tokyo"] < first["ANA"]


def test_the_transferred_band_sits_between_calibrated_and_blind():
    def first_band(trends):
        point = next(iter(trends.values())).projected[0]
        return (point.upper_usd - point.lower_usd) / point.price_usd

    blind = first_band(build_trend(ORIGIN, "SFO", DEPARTURE))
    _seed_other_departure()
    transferred = first_band(build_trend(ORIGIN, DESTINATION, DEPARTURE))
    _seed(_on_curve(range(9, -1, -1)))
    calibrated = first_band(build_trend(ORIGIN, DESTINATION, DEPARTURE))

    # 確かさの順序が、そのまま帯の広さの順序になる。
    assert calibrated < transferred < blind


def test_stale_observations_do_not_name_an_airline(monkeypatch):
    monkeypatch.setenv("AIRLINE_EVIDENCE_DAYS", "30")
    get_settings.cache_clear()
    # 200日前にしか観測されていない = 撤退した可能性がある。
    _seed_other_departure(days=range(200, 199, -1))

    trends = build_trend(ORIGIN, DESTINATION, DEPARTURE)

    assert all(t.basis == "simulated" for t in trends.values())
    get_settings.cache_clear()


def test_transferred_estimates_still_withhold_the_verdict():
    _seed_other_departure()

    result = recommend(ORIGIN, DESTINATION, DEPARTURE, today=TODAY)

    # 社名が出せても、この出発日の実売価格は1件も無い。比べる相手がいない。
    assert result["basis"] == "transferred"
    assert result["verdict"] == VERDICT_NO_DATA
    assert result["current_best"] is None
    assert result["evidence_points"] == 5


def test_this_departure_date_wins_over_transferred_evidence():
    _seed_other_departure(price=2200.0, airline="ANA")
    _seed(_on_curve(range(2, -1, -1)))

    trends = build_trend(ORIGIN, DESTINATION, DEPARTURE)

    assert all(t.basis == "observed" for t in trends.values())


def test_transferred_estimates_never_reach_the_prediction_log():
    _seed_other_departure()
    assert log_predictions(ORIGIN, DESTINATION, DEPARTURE, predicted_on=TODAY) == 0


def test_simulated_rows_from_another_departure_date_are_ignored():
    _seed({TODAY: 1.0}, airline="ZIPAIR Tokyo", source="simulated",
          departure=OTHER_DEPARTURE)

    trends = build_trend(ORIGIN, DESTINATION, DEPARTURE)

    # source='real' に限る約束は、出発日をまたぐ経路でも同じ。
    assert all(t.basis == "simulated" for t in trends.values())


def test_a_filter_that_matches_nothing_returns_nothing():
    # 匿名の曲線を返してはいけない。「そのルートを飛んでいない社にしぼった」
    # のに価格が出る（実測の最安 $708 に対し匿名推定 $448 が出た実例）。
    assert build_trend(ORIGIN, DESTINATION, DEPARTURE,
                       airline_filter=["Nonexistent Air"]) == {}
    assert recommend(ORIGIN, DESTINATION, DEPARTURE, today=TODAY,
                     airline_filter=["Nonexistent Air"]) is None


def test_a_filter_still_allows_the_transferred_estimate():
    _seed_other_departure(airline="ANA")

    trends = build_trend(ORIGIN, DESTINATION, DEPARTURE, airline_filter=["ANA"])

    # 観測された社にしぼった場合は、その社の推定を出してよい。
    assert {t.airline for t in trends.values()} == {"ANA"}


def test_airline_filter_narrows_the_trend():
    _seed(_on_curve(range(4, -1, -1)), airline="ANA")
    _seed(_on_curve(range(4, -1, -1), factor=0.6), airline="ZIPAIR")

    trends = build_trend(ORIGIN, DESTINATION, DEPARTURE,
                         airline_filter=["ZIPAIR"])

    assert {t.airline for t in trends.values()} == {"ZIPAIR"}


def test_oil_is_disabled_by_default_and_makes_no_network_call():
    from app.predict.oil import OilAdapter

    def explode(*_args, **_kwargs):
        raise AssertionError("係数0のときは EIA を叩いてはいけない")

    adapter = OilAdapter(opener=explode)
    assert adapter.adjustment_factor(TODAY) == 1.0


def test_oil_failures_degrade_to_a_neutral_factor(monkeypatch):
    from app.predict.oil import OilAdapter

    monkeypatch.setenv("OIL_COEFFICIENT", "0.5")
    monkeypatch.setenv("EIA_API_KEY", "dummy")
    get_settings.cache_clear()

    def explode(*_args, **_kwargs):
        raise OSError("network down")

    # 原油の取得失敗で価格予測そのものが止まってはいけない。
    assert OilAdapter(opener=explode).adjustment_factor(TODAY) == 1.0
    get_settings.cache_clear()


# ======================================================================
# prediction_log
# ======================================================================

def test_predictions_are_logged_at_the_configured_horizons():
    from app.config import PREDICTION_LOG_HORIZON_DAYS

    _seed(_on_curve(range(9, -1, -1)))
    written = log_predictions(ORIGIN, DESTINATION, DEPARTURE, predicted_on=TODAY)

    assert written == len(PREDICTION_LOG_HORIZON_DAYS)

    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM prediction_log")]
    targets = {r["target_check_date"] for r in rows}
    assert targets == {
        (TODAY + timedelta(days=h)).isoformat()
        for h in PREDICTION_LOG_HORIZON_DAYS
    }
    assert all(r["lower_usd"] < r["predicted_usd"] < r["upper_usd"] for r in rows)
    assert all(r["observed_points"] == 10 for r in rows)


def test_horizons_past_the_departure_date_are_not_logged():
    near = (TODAY + timedelta(days=5)).isoformat()
    _seed(_on_curve(range(4, -1, -1), departure=near), departure=near)

    written = log_predictions(ORIGIN, DESTINATION, near, predicted_on=TODAY)

    # 1日先と3日先だけが出発日に収まる。
    assert written == 2


def test_logging_is_idempotent_for_the_same_day():
    _seed(_on_curve(range(9, -1, -1)))
    log_predictions(ORIGIN, DESTINATION, DEPARTURE, predicted_on=TODAY)
    log_predictions(ORIGIN, DESTINATION, DEPARTURE, predicted_on=TODAY)

    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM prediction_log").fetchone()["c"]
    assert count == 5


def test_nothing_is_logged_without_history():
    assert log_predictions(ORIGIN, DESTINATION, DEPARTURE, predicted_on=TODAY) == 0


def test_curve_minimum_falls_inside_the_expected_low_window():
    """**グラフに敷く帯より外側が、帯の中より安く見えてはいけない。**

    価格トレンドのグラフは底値帯を縦の帯で敷く。帯の根拠は曲線パラメータ、
    線そのものは price_shape が決める。両者がずれると「帯の外のほうが安く
    見える」図になり、案内と絵が矛盾する。

    **等号を許すのが 2026-08-15 の変更点。** 実測で引き直した曲線は
    出発52〜300日前が平坦なので、最小値は「帯の中の1点」ではなく
    帯を含む広い台地になった。帯の外にも同じ安さの日がある状態が正しい。
    もはや帯は「ここが底」ではなく「ここより後は高くなる」の目印。
    """
    from datetime import date, timedelta

    from app.predict import simulator
    from app.predict.curve import load_params
    from app.predict.engine import expected_low_window

    params = load_params()
    today = date(2026, 8, 11)
    for departure in (date(2026, 12, 1), date(2027, 3, 11), date(2027, 6, 20)):
        window = expected_low_window(departure.isoformat(), today, params)
        assert window, departure

        points = []
        cursor = today
        while cursor <= departure:
            shape = simulator.price_shape("SAN", "HND", 1, "ANA",
                                          departure, cursor, params)
            if shape is not None:
                points.append((cursor.isoformat(), shape))
            cursor += timedelta(days=1)

        lowest = min(v for _, v in points)
        in_band = min(v for d, v in points
                      if window["start"] <= d <= window["end"])
        # 帯の中に、曲線全体の最小と同じ安さの日があること。
        # **帯の外に同点があるのは正しい**（平坦なので）。帯の中だけが
        # 明らかに高い、という図にならなければよい。
        assert in_band == pytest.approx(lowest, rel=1e-9), (
            f"{departure}: 帯の中の最安 {in_band} が全体の最安 {lowest} より高い")


# ======================================================================
# 値上がりが始まる日
#
# 2026-08-15 に曲線を実測から引き直した結果、利用者に伝えるべきものが
# 「待つべき窓」から「これを過ぎると上がる期限」に変わった。画面はこの
# 値だけを読む。
# ======================================================================

def test_the_rise_date_sits_at_the_end_of_the_flat_stretch():
    """**曲線が平坦をやめる日を返す。** 日数を直書きしない。"""
    from app.predict.curve import load_params
    from app.predict.engine import price_rises_after

    params = load_params()
    far = (TODAY + timedelta(days=300)).isoformat()

    rise = price_rises_after(far, TODAY)

    departure = date.fromisoformat(far)
    assert (departure - date.fromisoformat(rise["date"])).days == \
        params.sweet_spot_start_days
    assert rise["days_before_departure"] == params.sweet_spot_start_days


def test_the_rise_date_counts_down_and_goes_negative():
    """すでに上昇局面なら負の日数を返す。**None にしない** — 画面は
    「もう上がっている」と言う必要があり、無いと黙ってしまう。"""
    from app.predict.engine import price_rises_after
    from app.predict.curve import load_params

    lead = load_params().sweet_spot_start_days
    ahead = price_rises_after((TODAY + timedelta(days=lead + 10)).isoformat(), TODAY)
    inside = price_rises_after((TODAY + timedelta(days=lead - 10)).isoformat(), TODAY)

    assert ahead["days"] == 10
    assert inside["days"] == -10


def test_no_rise_date_once_departed():
    from app.predict.engine import price_rises_after

    past = (TODAY - timedelta(days=1)).isoformat()
    assert price_rises_after(past, TODAY) is None


def test_the_penalty_is_reported_so_the_screen_can_name_a_number():
    """「遅らせるとどれだけ損か」を画面が数字で言えること。

    これは曲線で唯一、実測が強く支持した性質（35/41系列, z=+4.5）。
    """
    from app.predict.engine import LATE_BOOKING_PENALTY_PCT, price_rises_after

    rise = price_rises_after((TODAY + timedelta(days=200)).isoformat(), TODAY)

    assert rise["penalty_pct"] == LATE_BOOKING_PENALTY_PCT
    assert rise["penalty_pct"] > 10


def test_recommendation_carries_the_rise_date():
    """実測ゼロでも出る。**較正に依存しない**（曲線の形だけで決まる）。"""
    result = recommend(ORIGIN, DESTINATION, DEPARTURE, today=TODAY)

    assert result["price_rises_after"]["date"]


# ============================================================
# 曲線の直前の形（2026-08-24 に引き直した）
# ============================================================

def test_the_last_minute_ramp_is_gentle():
    """**便ごとの予測に、ルート最安の急騰を当てない。**

    旧値は出発当日に 2.5倍としていたが、これは price_history
    （便を特定しない最安値）から取った形だった。安い便が売り切れれば
    残った便が動かなくても最安値は上がるので、便ごとに当てると外す。

    実測突き合わせで出発14日以内の予測が +16.9% 高かった
    （docs/prediction-accuracy-2026-08-24.md）。便ごとの系列3,952本を
    復元すると直前もほぼ平坦で、5日前で約1.06。
    """
    from app.predict.curve import load_params
    from app.predict.simulator import proximity_factor
    p = load_params()

    # 出発30日前まではほぼ動かない。
    assert 0.99 <= proximity_factor(30, p) <= 1.02
    assert 0.99 <= proximity_factor(18, p) <= 1.02
    # 直前は上がるが、緩やか。
    assert 1.02 <= proximity_factor(5, p) <= 1.12
    assert 1.05 <= proximity_factor(0, p) <= 1.15
    # **2倍を超える想定に戻さない。**
    assert proximity_factor(0, p) < 1.3, "直前の急騰が復活している"


def test_the_curve_never_doubles_within_the_last_month():
    """1か月の中で価格が倍になる形は、便ごとの実測に反する。"""
    from app.predict.curve import load_params
    from app.predict.simulator import proximity_factor
    p = load_params()
    at_30 = proximity_factor(30, p)
    assert proximity_factor(0, p) / at_30 < 1.25


# ── 出発当日に「待つ」と言わない（2026-10-17）──────────────────────
#
# 「これから先の最安」に今日の予測点を入れていたため、出発当日でも
# 「今日のモデル上の価格 < 今日の実売」だけで「待つ」が出ていた。

def test_no_wait_on_the_day_of_departure():
    today_dep = TODAY.isoformat()
    # 実売が曲線より高い（モデル上は今日がもっと安い）状態を作る
    _seed(_on_curve(range(9, -1, -1), factor=1.3, departure=today_dep),
          departure=today_dep)
    result = recommend(ORIGIN, DESTINATION, today_dep, today=TODAY)
    assert result["verdict"] != VERDICT_WAIT
    assert result["predicted_min"] is None


def test_the_predicted_minimum_is_never_today():
    _seed(_on_curve(range(9, -1, -1)))
    result = recommend(ORIGIN, DESTINATION, DEPARTURE, today=TODAY)
    pm = result["predicted_min"]
    assert pm is None or pm["check_date"] > TODAY.isoformat()
