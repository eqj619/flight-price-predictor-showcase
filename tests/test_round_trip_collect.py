"""往復対応・段階B — 収集。

ここで押さえるのは3つ。

1. 往復でも**1ルート1クエリ**であること（枠の消費が片道と同じ）
2. 収集した行に return_date が入り、片道と混ざらないこと
3. **応答の非対称**を実装が正しく扱っていること — 価格は往復合計だが
   便の情報は往路だけ、という取得元の都合
"""
from datetime import date, timedelta

import pytest

from app.collect import collector
from app.collect.provider import PriceProvider, ProviderError, Quote
from app.db import repository as repo


class Recorder(PriceProvider):
    """呼ばれ方を記録するだけの provider。"""
    name = "recorder"

    def __init__(self, price=600.0):
        self.calls = []
        self.price = price

    def fetch(self, origin, destination, departure_date, stops, airlines=None,
              return_date=None):
        self.calls.append({"origin": origin, "destination": destination,
                           "departure_date": departure_date, "stops": stops,
                           "return_date": return_date})
        if stops != 0 or airlines:
            return []
        return [Quote(airline="ANA", stops=0, via_airport=None,
                      price_usd=self.price, total_minutes=600)]


# ============================================================
# 1ルート1クエリであること
# ============================================================

def test_a_round_trip_costs_the_same_number_of_queries_as_a_one_way():
    """**往復を片道2本に分けて投げない。**

    往復運賃は片道の合成では出せない（NRT-LAX の ANA は片道 $2,164 に対し
    往復 $2,226。2026-08-19 実測）ので、往復として1回引く。枠の消費が
    片道と同じであることは容量計画の前提になっている。
    """
    one_way = Recorder()
    collector.collect_route(one_way, "NRT", "LAX", "2026-11-10",
                            date(2026, 8, 19), "2026-08-19T00:00")

    round_trip = Recorder()
    collector.collect_route(round_trip, "NRT", "LAX", "2026-11-10",
                            date(2026, 8, 19), "2026-08-19T00:00",
                            return_date="2026-11-20")

    assert len(round_trip.calls) == len(one_way.calls)


def test_the_return_date_reaches_the_provider_as_a_date():
    rec = Recorder()
    collector.collect_route(rec, "NRT", "LAX", "2026-11-10",
                            date(2026, 8, 19), "2026-08-19T00:00",
                            return_date="2026-11-20")
    assert all(c["return_date"] == date(2026, 11, 20) for c in rec.calls)


def test_an_empty_return_date_means_one_way():
    """既定は片道。**片道しか扱わなかった頃の呼び出しがそのまま通ること。**"""
    rec = Recorder()
    collector.collect_route(rec, "NRT", "LAX", "2026-11-10",
                            date(2026, 8, 19), "2026-08-19T00:00")
    assert all(c["return_date"] is None for c in rec.calls)


# ============================================================
# 収集した行が混ざらないこと
# ============================================================

def test_collected_rows_carry_the_return_date():
    rec = Recorder()
    snaps, _, _ = collector.collect_route(
        rec, "NRT", "LAX", "2026-11-10", date(2026, 8, 19),
        "2026-08-19T00:00", return_date="2026-11-20")
    assert snaps and all(s.return_date == "2026-11-20" for s in snaps)


def test_one_way_and_round_trip_collections_land_side_by_side():
    """同じ区間・同じ出発日を両方収集しても、互いを潰さない。"""
    for ret, price in (("", 2164.0), ("2026-11-20", 2226.0)):
        snaps, _, _ = collector.collect_route(
            Recorder(price), "NRT", "LAX", "2026-11-10", date(2026, 8, 19),
            "2026-08-19T00:00", return_date=ret)
        repo.upsert_snapshots(snaps)

    assert [r["price_usd"] for r in repo.history_for("NRT", "LAX", "2026-11-10")] \
        == [2164.0]
    assert [r["price_usd"] for r in repo.history_for("NRT", "LAX", "2026-11-10",
                                                     return_date="2026-11-20")] \
        == [2226.0]


def test_collect_once_uses_each_routes_own_return_date():
    repo.upsert_monitored_route("NRT", "LAX", "2036-11-10")
    repo.upsert_monitored_route("NRT", "LAX", "2036-11-10", "2036-11-20")

    rec = Recorder()
    collector.collect_once(rec, check_date=date(2026, 8, 19))

    seen = {c["return_date"] for c in rec.calls}
    assert seen == {None, date(2036, 11, 20)}


# ============================================================
# 失敗の記録も往復を区別すること
# ============================================================

def test_fetch_errors_record_which_trip_type_failed():
    class Broken(PriceProvider):
        name = "broken"
        def fetch(self, *a, **k):
            raise ProviderError("boom")

    repo.upsert_monitored_route("NRT", "LAX", "2036-11-10", "2036-11-20")
    collector.collect_once(Broken(), check_date=date(2026, 8, 19))

    rows = repo.fetch_error_summary("2026-01-01")
    assert rows, "失敗が記録されていない"


# ============================================================
# 応答の非対称（取得元の都合。実装ではなく事実の記録）
# ============================================================

def test_quote_detail_fields_describe_the_outbound_leg_only():
    """**往復の Quote は「価格は往復合計、便の情報は往路だけ」。**

    Google Flights の往路選択画面がそう見せているため、復路のセグメントは
    応答に入っていない（2026-08-19 実測）。したがって total_minutes を
    往復の総所要として読んではいけない。

    ここでは実装が勝手に補完していないこと（往路の値をそのまま持ち帰る）を
    確かめる。**取得元が直らない限りこの非対称は残る。**
    """
    rec = Recorder()
    snaps, _, _ = collector.collect_route(
        rec, "NRT", "LAX", "2026-11-10", date(2026, 8, 19),
        "2026-08-19T00:00", return_date="2026-11-20")

    assert snaps[0].total_minutes == 600      # 往路のぶんだけ
    assert snaps[0].stops == 0                # 往路の経由回数


# ============================================================
# 調査用モニターの一括登録（往復）
# ============================================================

def _run_cli(*argv):
    from app import cli
    return cli.main(list(argv))


def test_research_monitors_create_one_route_per_stay_length():
    """滞在日数のぶんだけ別ルートになる。**同じ出発日でも潰し合わない。**"""
    _run_cli("add-research-monitors", "--routes", "SAN-HND",
             "--from-days", "90", "--to-days", "100", "--count", "2",
             "--stay-nights", "7,14")

    routes = repo.active_routes()
    assert len(routes) == 4          # 1区間 × 2出発日 × 2滞在
    assert all(r.return_date for r in routes), "片道が混ざっている"

    # 出発日ごとに、滞在7泊と14泊の2本がある。
    by_departure: dict[str, set[str]] = {}
    for r in routes:
        by_departure.setdefault(r.departure_date, set()).add(r.return_date)
    for departure, returns in by_departure.items():
        d = date.fromisoformat(departure)
        assert returns == {(d + timedelta(days=7)).isoformat(),
                           (d + timedelta(days=14)).isoformat()}


def test_research_monitors_stay_one_way_without_the_flag():
    """**既存の使い方を変えない。** --stay-nights を渡さなければ片道。"""
    _run_cli("add-research-monitors", "--routes", "SAN-HND",
             "--from-days", "90", "--to-days", "100", "--count", "2")
    routes = repo.active_routes()
    assert len(routes) == 2
    assert all(r.return_date == "" for r in routes)


def test_research_round_trips_do_not_collide_with_the_one_way_study():
    """同じ区間・同じ出発日に片道の調査が既にあっても、往復は別に建つ。"""
    _run_cli("add-research-monitors", "--routes", "SAN-HND",
             "--from-days", "90", "--to-days", "91", "--count", "1")
    _run_cli("add-research-monitors", "--routes", "SAN-HND",
             "--from-days", "90", "--to-days", "91", "--count", "1",
             "--stay-nights", "7")

    routes = repo.active_routes()
    assert len(routes) == 2
    assert {r.return_date == "" for r in routes} == {True, False}
