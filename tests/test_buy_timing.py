"""買い時（底値の時期）の答え合わせ（2026-10-12）。

**アプリの目的は「いま底値の時期か」を言い当てること。** それまでダッシュ
ボードの精度欄は「明日の価格を何%で当てたか」しか出していなかった。ここでは
数字を手で確かめられる小さな系列を作って、3つの集計を固定する。
"""
from datetime import date, timedelta

import pytest

from app import buy_timing
from app.db import repository

TODAY = date(2026, 10, 12)


def _path(origin, destination, departure: date, prices: dict[int, float]):
    """出発日を迎えた系列を作る。prices は {出発までの日数: その日の最安}。"""
    repository.upsert_monitored_route(origin, destination, departure.isoformat())
    repository.upsert_snapshots([
        repository.Snapshot(
            check_date=(departure - timedelta(days=dbd)).isoformat(),
            departure_date=departure.isoformat(), origin=origin,
            destination=destination, stops=1, via_airport="LAX", airline="ANA",
            price_usd=p, source="real", fetched_at="2026-10-01T03:00:00")
        for dbd, p in prices.items()])


def _v_shaped(bottom_day, low=500.0):
    """出発90日前から当日まで。bottom_day で最安、そこから離れるほど高い。"""
    return {d: low + 5 * abs(d - bottom_day) for d in range(0, 91)}


DEP = TODAY - timedelta(days=3)          # 出発済み


def test_a_series_too_short_to_judge_is_left_out():
    """**観測の始まりより前の底は見えない。** 60日前から追えていない系列は数えない。"""
    _path("HND", "LAX", DEP, {d: 600.0 for d in range(0, 40)})
    result = buy_timing.evaluate(TODAY)
    assert result["series"] == 0
    assert result["series_all"] == 1


def test_the_bottom_is_placed_in_the_right_bucket():
    _path("HND", "LAX", DEP, _v_shaped(65))
    _path("NRT", "LAX", DEP, _v_shaped(55))
    result = buy_timing.evaluate(TODAY)

    by = {b["label"]: b["n"] for b in result["bottoms"]}
    assert result["series"] == 2
    assert by["60-75"] == 1
    valley = next(b for b in result["bottoms"] if b["valley"])
    assert valley["n"] == 1, "55日前の底がアプリの谷（52-59）に入っていない"


def test_a_flat_bottom_counts_its_last_day():
    """**底が何日も続いたら最後の日。** その値段で買えた最後の機会なので。"""
    prices = {d: 700.0 for d in range(0, 91)}
    for d in range(56, 70):
        prices[d] = 500.0                       # 56〜69日前がずっと最安
    _path("HND", "LAX", DEP, prices)

    valley = next(b for b in buy_timing.evaluate(TODAY)["bottoms"] if b["valley"])
    assert valley["n"] == 1, "最後の日（56日前）で数えていない"


def test_buying_on_a_given_day_is_measured_against_the_bottom():
    """55日前に底 $500。52日前に買うと 500+5*3=$515 → 底値より +3.0%。"""
    _path("HND", "LAX", DEP, _v_shaped(55))
    days = {b["dbd"]: b for b in buy_timing.evaluate(TODAY)["buy_days"]}

    assert days[55]["median_pct"] == 0.0
    assert days[52]["median_pct"] == pytest.approx(3.0)
    assert days[52]["within_pct"] == 100            # 5%以内
    assert days[7]["median_pct"] == pytest.approx(48.0)   # 500+5*48=$740
    assert days[7]["within_pct"] == 0


def test_the_best_day_is_the_one_with_the_lowest_average():
    _path("HND", "LAX", DEP, _v_shaped(60))
    _path("NRT", "LAX", DEP, _v_shaped(60))
    assert buy_timing.evaluate(TODAY)["best_dbd"] == 60


def test_the_valley_follows_the_curve_parameters():
    """**区切りは曲線の値から作る。** 引き直して谷が動いたら、画面も動く。"""
    from app.predict.curve import load_params
    p = load_params()
    result = buy_timing.evaluate(TODAY)
    assert result["valley"] == [p.sweet_spot_start_days, p.sweet_spot_end_days]
    assert result["rise_dbd"] == p.sweet_spot_start_days


# ── 判定の答え合わせ ──────────────────────────────────────

def _verdict(departure: date, dbd: int, verdict: str, price: float):
    repository.upsert_verdict(
        logged_on=(departure - timedelta(days=dbd)).isoformat(),
        origin="HND", destination="LAX", departure_date=departure.isoformat(),
        days_before_departure=dbd, verdict=verdict, current_price_usd=price)


def test_buy_now_is_right_when_nothing_much_cheaper_came_later(monkeypatch):
    monkeypatch.setattr("app.clock.today", lambda: TODAY)
    _path("HND", "LAX", DEP, _v_shaped(55))
    _verdict(DEP, 55, "buy_now", 500.0)     # 底の日に「今買う」→ 当たり
    _verdict(DEP, 80, "buy_now", 625.0)     # まだ下がる日に「今買う」→ 外れ

    v = buy_timing.evaluate(TODAY)["verdicts"]["by_verdict"]["buy_now"]
    assert v["n"] == 2
    assert v["hit_pct"] == 50


def test_wait_is_right_only_when_a_lower_price_actually_came(monkeypatch):
    monkeypatch.setattr("app.clock.today", lambda: TODAY)
    _path("HND", "LAX", DEP, _v_shaped(55))
    _verdict(DEP, 80, "wait", 625.0)        # 後で $500 まで下がった → 当たり
    _verdict(DEP, 40, "wait", 575.0)        # この後は上がるだけ → 外れ

    v = buy_timing.evaluate(TODAY)["verdicts"]["by_verdict"]["wait"]
    assert v["n"] == 2
    assert v["hit_pct"] == 50


def test_no_verdicts_yet_is_reported_as_such(monkeypatch):
    """**記録を始めたばかりで、答え合わせできる判定がまだ無い**状態を正直に出す。"""
    monkeypatch.setattr("app.clock.today", lambda: TODAY)
    v = buy_timing.evaluate(TODAY)["verdicts"]
    assert v["evaluable"] == 0
    assert v["by_verdict"] == {}


# ── 予測した底値の日の答え合わせ（2026-10-12 追加）──────────────

def _verdict_with_min(departure: date, dbd: int, pred_dbd: int, price: float):
    repository.upsert_verdict(
        logged_on=(departure - timedelta(days=dbd)).isoformat(),
        origin="HND", destination="LAX", departure_date=departure.isoformat(),
        days_before_departure=dbd, verdict="wait", current_price_usd=price,
        predicted_min_date=(departure - timedelta(days=pred_dbd)).isoformat(),
        predicted_min_usd=500.0)


def test_a_correct_bottom_date_costs_nothing(monkeypatch):
    """底が55日前の便で、80日前に「底は55日前」と予測 → ずれ0日、差0%。"""
    monkeypatch.setattr("app.clock.today", lambda: TODAY)
    _path("HND", "LAX", DEP, _v_shaped(55))
    _verdict_with_min(DEP, 80, 55, 625.0)

    t = buy_timing.evaluate(TODAY)["timing"]
    assert t["n"] == 1 and t["series"] == 1
    assert t["day_err_median"] == 0
    assert t["within_pct"] == 100
    assert t["regret"]["app"]["median"] == 0.0
    # すぐ買う: 625/500 → +25%。アプリの期限（52日前）: 515/500 → +3%
    assert t["regret"]["now"]["median"] == 25.0
    assert t["regret"]["deadline"]["median"] == 3.0
    assert t["beat_deadline_pct"] == 100


def test_a_bottom_predicted_too_early_is_signed_positive(monkeypatch):
    """＋は「実際より早すぎ（出発から遠すぎ）」。"""
    monkeypatch.setattr("app.clock.today", lambda: TODAY)
    _path("HND", "LAX", DEP, _v_shaped(40))
    _verdict_with_min(DEP, 80, 60, 700.0)

    t = buy_timing.evaluate(TODAY)["timing"]
    assert t["day_err_median"] == 20
    assert t["early_pct"] == 100
    assert t["regret"]["app"]["median"] == 20.0     # 600/500


def test_only_prices_after_the_call_count(monkeypatch):
    """**過去には戻って買えない。** 判定の日より前の底は比べる相手に入れない。"""
    monkeypatch.setattr("app.clock.today", lambda: TODAY)
    _path("HND", "LAX", DEP, _v_shaped(70))
    # 50日前の判定。この時点から先の最安は50日前当日（600）。予測も50日前。
    _verdict_with_min(DEP, 50, 50, 600.0)

    t = buy_timing.evaluate(TODAY)["timing"]
    assert t["regret"]["app"]["median"] == 0.0
    assert t["day_err_median"] == 0


def test_no_timing_yet_is_reported_as_such(monkeypatch):
    monkeypatch.setattr("app.clock.today", lambda: TODAY)
    assert buy_timing.evaluate(TODAY)["timing"]["n"] == 0



# ── 方向別（2026-10-14、課題16）────────────────────────────────

def test_buy_days_are_split_by_direction():
    """日本発と米国発は別々に数え、期限もその方向の値を出す。"""
    _path("NRT", "LAX", DEP, _v_shaped(70))     # 日本発: 底は70日前
    _path("LAX", "NRT", DEP, _v_shaped(20))     # 米国発: 底は20日前
    by = {d["direction"]: d for d in buy_timing.evaluate(TODAY)["by_direction"]}

    jp, us = by["JP->US-West"], by["US-West->JP"]
    assert jp["series"] == 1 and us["series"] == 1
    assert jp["rise_dbd"] == 65 and us["rise_dbd"] == 52
    at = lambda d, dbd: next(b for b in d["buy_days"] if b["dbd"] == dbd)
    assert at(jp, 75)["median_pct"] == 5.0      # 525/500
    assert at(us, 21)["median_pct"] == 1.0      # 505/500


def test_verdicts_are_graded_per_direction(monkeypatch):
    """判定の答え合わせも方向別に数える。どちらでもない路線は全体にだけ入る。"""
    monkeypatch.setattr("app.clock.today", lambda: TODAY)
    logged = (DEP - timedelta(days=20)).isoformat()
    for o, d, verdict in (("NRT", "LAX", "buy_now"), ("LAX", "NRT", "wait"),
                          ("LAX", "SFO", "buy_now")):
        _path(o, d, DEP, _v_shaped(5))          # 底は5日前。20日前より安くなる
        repository.upsert_verdict(
            logged_on=logged, origin=o, destination=d,
            departure_date=DEP.isoformat(), return_date="",
            days_before_departure=20, verdict=verdict, current_price_usd=575.0)
    result = buy_timing.evaluate(TODAY)
    by = {x["direction"]: x["verdicts"] for x in result["by_direction"]}

    assert result["verdicts"]["evaluable"] == 3
    jp, us = by["JP->US-West"], by["US-West->JP"]
    assert jp["evaluable"] == 1 and jp["flights"] == 1
    assert jp["by_verdict"]["buy_now"]["hit_pct"] == 0      # その後 13% 下がった
    assert us["evaluable"] == 1
    assert us["by_verdict"]["wait"]["hit_pct"] == 100       # 下がると言って下がった


def test_bottoms_use_each_directions_own_window():
    """谷は方向ごとの値。区切りは重ならず、どの系列も1回だけ数える。"""
    for dep_offset, bottom in enumerate((70, 62, 40, 55)):
        _path("NRT", "LAX", DEP - timedelta(days=dep_offset), _v_shaped(bottom))
    _path("LAX", "NRT", DEP, _v_shaped(55))
    by = {d["direction"]: d for d in buy_timing.evaluate(TODAY)["by_direction"]}

    jp, us = by["JP->US-West"], by["US-West->JP"]
    assert jp["valley"] == [65, 75] and us["valley"] == [52, 59]
    assert sum(b["n"] for b in jp["bottoms"]) == 4          # 重なれば 4 を超える
    n = {b["label"]: b["n"] for b in jp["bottoms"]}
    assert n["65-75"] == 1 and n["46-64"] == 2 and n["31-45"] == 1
    assert next(b for b in us["bottoms"] if b["valley"])["n"] == 1
    assert jp["valley_observed"] == 4


def test_series_that_start_inside_the_window_are_counted_as_not_observed():
    """谷の初めの日まで観測が届かない系列は、谷の前の底が見えない。"""
    _path("NRT", "LAX", DEP, {d: 500.0 + d for d in range(0, 70)})    # 69日前から
    jp = next(d for d in buy_timing.evaluate(TODAY)["by_direction"]
              if d["direction"] == "JP->US-West")
    assert jp["series"] == 1 and jp["valley_observed"] == 0


def test_timing_is_graded_per_direction_with_its_own_deadline(monkeypatch):
    """予測した日の答え合わせも方向別。比べる期限はその方向の値。"""
    monkeypatch.setattr("app.clock.today", lambda: TODAY)
    for o, d in (("NRT", "LAX"), ("LAX", "NRT")):
        _path(o, d, DEP, _v_shaped(55))
        repository.upsert_verdict(
            logged_on=(DEP - timedelta(days=80)).isoformat(), origin=o, destination=d,
            departure_date=DEP.isoformat(), days_before_departure=80, verdict="wait",
            current_price_usd=625.0,
            predicted_min_date=(DEP - timedelta(days=55)).isoformat(),
            predicted_min_usd=500.0)
    by = {x["direction"]: x["timing"] for x in buy_timing.evaluate(TODAY)["by_direction"]}

    jp, us = by["JP->US-West"], by["US-West->JP"]
    assert jp["n"] == 1 and us["n"] == 1
    assert jp["rise_dbd"] == 65 and us["rise_dbd"] == 52
    assert jp["regret"]["deadline"]["median"] == 10.0       # 65日前: 550/500
    assert us["regret"]["deadline"]["median"] == 3.0        # 52日前: 515/500



# ── 取得元の過去価格は混ぜない（2026-10-14）────────────────────

def test_the_provider_price_history_is_left_out():
    """**price_history は自前の実測より安く出る別の量。** 混ぜると遠い日が
    安く見えて、早く買うほど得という結論が作られてしまう。"""
    _path("HND", "LAX", DEP, _v_shaped(30))            # 自前の底は30日前・$500
    with repository.get_connection() as conn:
        conn.execute(
            "INSERT INTO price_history (origin, destination, departure_date, "
            "return_date, observed_date, price_usd, captured_at) "
            "VALUES ('HND','LAX',?, '', ?, 100.0, '2026-08-01T00:00:00')",
            (DEP.isoformat(), (DEP - timedelta(days=80)).isoformat()))
    series = repository.completed_one_way_price_paths(TODAY.isoformat())
    assert min(series[("HND", "LAX", DEP.isoformat())].values()) == 500.0


# ── 出発直前の判定は答え合わせに数えない（2026-10-17）────────────

def test_calls_made_in_the_last_two_days_are_not_graded(monkeypatch):
    """出発当日・前日の判定には「その後」が無い。待つは必ず外れになる。"""
    monkeypatch.setattr("app.clock.today", lambda: TODAY)
    _path("HND", "LAX", DEP, _v_shaped(55))
    _verdict(DEP, 1, "wait", 500.0)
    _verdict(DEP, 0, "wait", 500.0)
    _verdict(DEP, 40, "wait", 575.0)
    v = buy_timing.evaluate(TODAY)["verdicts"]
    assert v["evaluable"] == 1
