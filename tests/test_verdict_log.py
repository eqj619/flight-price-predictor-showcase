"""買い時の判定を毎日記録する（2026-10-12）。

**アプリの目的は「いま底値の時期か」を言い当てること**（docs/issues.md 冒頭）。
それまで記録していたのは価格の点予測だけで、今買う／待つ／様子見の判定は
一度も答え合わせしていなかった。出発日を迎えたら、判定した日の価格が底値に
どれだけ近かったかを測るために残す。
"""
from datetime import date, timedelta

from app.db import repository
from app.db.connection import get_connection
from app.jobs.daily import _log_verdicts

TODAY = date(2026, 10, 12)
DEP = (TODAY + timedelta(days=90)).isoformat()


def _observed(origin, destination, departure=DEP, days=8, price=700.0,
              return_date=""):
    """何日分かの実測を作る（判定が出る状態にする）。"""
    repository.upsert_monitored_route(origin, destination, departure, return_date)
    repository.upsert_snapshots([
        repository.Snapshot(
            check_date=(TODAY - timedelta(days=i)).isoformat(),
            departure_date=departure, return_date=return_date,
            origin=origin, destination=destination, stops=1,
            via_airport="LAX", airline="ANA", price_usd=price + i,
            source="real", fetched_at="2026-10-12T03:00:00")
        for i in range(days)])


def _verdicts():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM verdict_log ORDER BY origin, destination")]


def test_a_route_with_observations_gets_its_verdict_recorded():
    _observed("SAN", "HND")

    assert _log_verdicts(TODAY) == 1
    rows = _verdicts()
    assert len(rows) == 1
    row = rows[0]
    assert row["logged_on"] == TODAY.isoformat()
    assert row["verdict"] in ("buy_now", "wait", "watch")
    assert row["days_before_departure"] == 90
    assert row["current_price_usd"] is not None, "答え合わせに要る判定日の価格が無い"
    assert row["rise_date"], "画面が出している値上がり開始日も残す"


def test_running_twice_on_the_same_day_does_not_count_twice():
    """**同じ日に流し直しても1件。** 二重に数えると的中率が歪む。"""
    _observed("SAN", "HND")
    _log_verdicts(TODAY)
    _log_verdicts(TODAY)

    assert len(_verdicts()) == 1


def test_round_trips_are_not_recorded():
    """往復には判定を出していない（曲線が往復で未検証）。記録するものが無い。"""
    _observed("SAN", "HND", return_date=(TODAY + timedelta(days=100)).isoformat())

    assert _log_verdicts(TODAY) == 0
    assert _verdicts() == []


def test_one_failing_route_does_not_stop_the_others(monkeypatch):
    _observed("SAN", "HND")
    _observed("LAX", "NRT")
    from app.predict import engine
    real = engine.recommend

    def flaky(origin, destination, *a, **k):
        if origin == "SAN":
            raise RuntimeError("boom")
        return real(origin, destination, *a, **k)
    monkeypatch.setattr(engine, "recommend", flaky)

    assert _log_verdicts(TODAY) == 1
    assert [r["origin"] for r in _verdicts()] == ["LAX"]
