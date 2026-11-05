"""予測の答え合わせと精度（課題9）。

**主題は「測れているか」ではなく「測り方が正しいか」。**
誤った測り方で「精度が良い」と出るのが一番まずい — 曲線を引き直したときに
悪化したことに気づけなくなる。
"""
from datetime import date, timedelta

import pytest

from app import accuracy
from app.db import repository as repo

TODAY = date(2026, 8, 24)
DEP = "2026-09-30"


def _prediction(target, predicted, lower, upper, predicted_on="2026-08-20",
                airline="ANA", stops=0, via=None, return_date=""):
    repo.insert_predictions([repo.Prediction(
        predicted_on=predicted_on, target_check_date=target, departure_date=DEP,
        origin="NRT", destination="LAX", stops=stops, via_airport=via,
        airline=airline, predicted_usd=predicted, lower_usd=lower,
        upper_usd=upper, observed_points=5, return_date=return_date)])


def _actual(check_date, price, airline="ANA", stops=0, via=None, return_date=""):
    repo.upsert_snapshots([repo.Snapshot(
        check_date=check_date, departure_date=DEP, origin="NRT",
        destination="LAX", stops=stops, via_airport=via, airline=airline,
        price_usd=price, source="real", fetched_at="x",
        return_date=return_date)])


# ============================================================
# 採点
# ============================================================

def test_a_prediction_is_scored_once_its_day_arrives():
    _prediction("2026-08-22", 550.0, 500.0, 600.0)
    _actual("2026-08-22", 500.0)

    assert repo.score_predictions(TODAY.isoformat()) == 1
    rows = repo.prediction_scores("2026-08-01")
    assert rows[0]["actual_usd"] == 500.0


def test_a_future_prediction_is_not_scored():
    _prediction("2026-09-20", 550.0, 500.0, 600.0)
    _actual("2026-09-20", 500.0)          # 未来の実測（ありえないが念のため）
    assert repo.score_predictions(TODAY.isoformat()) == 0


def test_scoring_is_idempotent():
    """日次ジョブが二度走っても二重に数えない。"""
    _prediction("2026-08-22", 550.0, 500.0, 600.0)
    _actual("2026-08-22", 500.0)
    assert repo.score_predictions(TODAY.isoformat()) == 1
    assert repo.score_predictions(TODAY.isoformat()) == 0


def test_an_unobserved_flight_is_left_unscored():
    """**「外した」と「比べる相手がいない」は違う。**

    その日その便が売られていなければ、予測が外れたわけではない。
    0 で埋めると精度が実際より悪く見える。
    """
    _prediction("2026-08-22", 550.0, 500.0, 600.0)
    # 同じ日に別の便だけ観測された
    _actual("2026-08-22", 500.0, airline="JAL")

    assert repo.score_predictions(TODAY.isoformat()) == 0
    assert repo.prediction_scores("2026-08-01") == []


def test_the_match_requires_the_same_flight():
    """便が違えば別の量。経由地まで一致させる。"""
    _prediction("2026-08-22", 550.0, 500.0, 600.0, via="ICN")
    _actual("2026-08-22", 500.0, via="SFO")
    assert repo.score_predictions(TODAY.isoformat()) == 0


def test_one_way_and_round_trip_are_not_matched():
    _prediction("2026-08-22", 550.0, 500.0, 600.0)
    _actual("2026-08-22", 1500.0, return_date="2026-10-10")
    assert repo.score_predictions(TODAY.isoformat()) == 0


# ============================================================
# 集計
# ============================================================

def test_bias_shows_the_direction_not_just_the_size():
    """**偏りを別に持つ理由。** 絶対誤差だけだと系統的な高すぎに
    気づけない（初回測定で +15.3% だった）。"""
    for i, target in enumerate(["2026-08-20", "2026-08-21", "2026-08-22"]):
        _prediction(target, 600.0, 550.0, 650.0, airline=f"AL{i}")
        _actual(target, 500.0, airline=f"AL{i}")     # 予測が2割高い
    repo.score_predictions(TODAY.isoformat())

    overall = accuracy.measure(TODAY)["overall"]
    assert overall["n"] == 3
    assert overall["abs_error_pct"] == 20.0
    assert overall["bias_pct"] == 20.0, "高すぎであることが出ていない"


def test_bias_is_negative_when_predictions_run_low():
    _prediction("2026-08-22", 400.0, 350.0, 450.0)
    _actual("2026-08-22", 500.0)
    repo.score_predictions(TODAY.isoformat())
    assert accuracy.measure(TODAY)["overall"]["bias_pct"] == -20.0


def test_band_coverage_counts_the_edges_as_inside():
    _prediction("2026-08-22", 550.0, 500.0, 600.0)
    _actual("2026-08-22", 500.0)          # 下端ちょうど
    repo.score_predictions(TODAY.isoformat())
    assert accuracy.measure(TODAY)["overall"]["band_hit_pct"] == 100.0


def test_a_miss_outside_the_band_is_counted():
    _prediction("2026-08-22", 550.0, 500.0, 600.0)
    _actual("2026-08-22", 450.0)
    repo.score_predictions(TODAY.isoformat())
    assert accuracy.measure(TODAY)["overall"]["band_hit_pct"] == 0.0


def test_results_are_split_by_lead_days():
    """誤差は先読みとともに伸びる。まとめると地平線の構成で動く。"""
    _prediction("2026-08-21", 500.0, 450.0, 550.0, predicted_on="2026-08-20")
    _actual("2026-08-21", 500.0)
    _prediction("2026-08-23", 700.0, 650.0, 750.0, predicted_on="2026-08-16",
                airline="JAL")
    _actual("2026-08-23", 500.0, airline="JAL")
    repo.score_predictions(TODAY.isoformat())

    by_lead = accuracy.measure(TODAY)["by_lead_days"]
    assert by_lead[1]["abs_error_pct"] == 0.0
    assert by_lead[7]["abs_error_pct"] == 40.0


def test_old_results_fall_out_of_the_window():
    """直近7日の窓。古い測定がいつまでも混ざらない。"""
    _prediction("2026-08-01", 600.0, 550.0, 650.0)
    _actual("2026-08-01", 500.0)
    repo.score_predictions(TODAY.isoformat())
    assert accuracy.measure(TODAY)["overall"]["n"] == 0
    assert accuracy.measure(TODAY, window_days=60)["overall"]["n"] == 1


def test_no_samples_reports_none_not_zero():
    """**0件と誤差0%は違う。** 空欄で返す。"""
    overall = accuracy.measure(TODAY)["overall"]
    assert overall["n"] == 0
    assert overall["abs_error_pct"] is None
    assert overall["band_hit_pct"] is None


# ============================================================
# 出発が遠い予測に薄められないこと
# ============================================================

def test_far_out_predictions_do_not_hide_the_near_departure_error():
    """**まとめた数字は嘘になる。**

    出発が遠いところは曲線が平坦で「直近の値をそのまま伸ばす」だけなので
    当たって当然。件数もそちらが多い。全体だけ見ていると、人が実際に買う
    直前で大きく外していることに気づけない（2026-08-24 の実測では全体
    0.5% に対し出発14日以内は 20.0%）。
    """
    # 出発が遠い予測を10件、ぴたりと当てる
    far_dep = "2027-06-01"
    for i in range(10):
        target = f"2026-08-2{i % 3}"
        repo.insert_predictions([repo.Prediction(
            predicted_on="2026-08-19", target_check_date=target,
            departure_date=far_dep, origin="NRT", destination="LAX",
            stops=0, via_airport=None, airline=f"FAR{i}",
            predicted_usd=500.0, lower_usd=450.0, upper_usd=550.0,
            observed_points=5)])
        repo.upsert_snapshots([repo.Snapshot(
            check_date=target, departure_date=far_dep, origin="NRT",
            destination="LAX", stops=0, via_airport=None, airline=f"FAR{i}",
            price_usd=500.0, source="real", fetched_at="x")])

    # 直前の予測を2件、2割高く外す
    near_dep = "2026-08-26"
    for i in range(2):
        target = f"2026-08-2{i}"
        repo.insert_predictions([repo.Prediction(
            predicted_on="2026-08-19", target_check_date=target,
            departure_date=near_dep, origin="NRT", destination="SFO",
            stops=0, via_airport=None, airline=f"NEAR{i}",
            predicted_usd=600.0, lower_usd=580.0, upper_usd=620.0,
            observed_points=5)])
        repo.upsert_snapshots([repo.Snapshot(
            check_date=target, departure_date=near_dep, origin="NRT",
            destination="SFO", stops=0, via_airport=None, airline=f"NEAR{i}",
            price_usd=500.0, source="real", fetched_at="x")])

    repo.score_predictions(TODAY.isoformat())
    result = accuracy.measure(TODAY)

    # 全体は薄まる。
    assert result["overall"]["abs_error_pct"] < 5.0
    # **直前だけを見れば外していることが分かる。**
    assert result["near_departure"]["n"] == 2
    assert result["near_departure"]["abs_error_pct"] == 20.0
    assert result["near_departure"]["bias_pct"] == 20.0
    assert result["near_departure"]["band_hit_pct"] == 0.0


def test_the_buckets_show_where_the_error_lives():
    repo.insert_predictions([repo.Prediction(
        predicted_on="2026-08-19", target_check_date="2026-08-22",
        departure_date="2026-08-26", origin="NRT", destination="LAX",
        stops=0, via_airport=None, airline="ANA",
        predicted_usd=600.0, lower_usd=550.0, upper_usd=650.0,
        observed_points=5)])
    repo.upsert_snapshots([repo.Snapshot(
        check_date="2026-08-22", departure_date="2026-08-26", origin="NRT",
        destination="LAX", stops=0, via_airport=None, airline="ANA",
        price_usd=500.0, source="real", fetched_at="x")])
    repo.score_predictions(TODAY.isoformat())

    buckets = accuracy.measure(TODAY)["by_days_before_departure"]
    assert "0-14" in buckets and buckets["0-14"]["n"] == 1
