"""収集ジョブのテスト。ネットワークは一切叩かない（FakeProvider）。

PriceProvider 抽象を入れた目的の半分はここにある。collector が fast_flights
を知らないので、収集ロジックだけを高速に検証できる。
"""
from datetime import date

import pytest

from app.collect.collector import collect_once, plan_queries
from app.collect.provider import PriceProvider, ProviderError, Quote
from app.config import get_settings
from app.db import repository
from app.db.connection import get_connection
from app.routing import expand_routes

DEPARTURE = "2026-12-20"
CHECK_DATE = date(2026, 8, 8)


class FakeProvider(PriceProvider):
    """呼ばれたクエリを記録し、あらかじめ仕込んだ結果を返す。"""

    name = "fake"

    def __init__(self, quotes=None, fail_routes=()):
        self.calls: list[tuple] = []
        self._quotes = quotes or {}
        self._fail_routes = set(fail_routes)

    def fetch(self, origin, destination, departure_date, stops, airlines=None,
              return_date=None):
        key = (origin, destination, stops,
               tuple(airlines) if airlines else None)
        self.calls.append(key)
        if (origin, destination) in self._fail_routes:
            raise ProviderError("simulated failure")
        return list(self._quotes.get(key, []))

    @property
    def routes_called(self) -> set[tuple[str, str]]:
        return {(o, d) for o, d, _s, _a in self.calls}


def _quote(price, airline, stops=0, via=None):
    return Quote(airline=airline, stops=stops, via_airport=via, price_usd=price)


def _register(origin: str, destination: str, departure_date: str = DEPARTURE):
    """モニターを1件登録する（Phase 4 の API がやることの最小版）。"""
    route_ids = [
        repository.upsert_monitored_route(o, d, departure_date)
        for o, d in expand_routes(origin, destination, nearby_airports=True)
    ]
    return repository.create_watch(origin, destination, departure_date, route_ids)


def _snapshot_rows():
    """テストからの検証用。アプリ側の SQL は repository に閉じ込めたまま、
    ここだけは生で覗く（fetch_errors 等は業務上の read API を持たないため）。"""
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM snapshots")]


def _fetch_error_rows():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM fetch_errors")]


# ------------------------------------------------------------------
# 基本
# ------------------------------------------------------------------

def test_collected_rows_are_marked_real():
    _register("KIX", "SFO")
    provider = FakeProvider({
        ("KIX", "SFO", 0, None): [_quote(700, "ANA")],
    })

    collect_once(provider, CHECK_DATE)

    rows = _snapshot_rows()
    assert rows and all(r["source"] == "real" for r in rows)
    # シミュレーション値が実測として紛れ込む経路を作らない（2.5 の欠陥4）。
    assert repository.history_for("KIX", "SFO", DEPARTURE)


def test_snapshot_carries_the_check_date_and_route():
    _register("KIX", "SFO")
    provider = FakeProvider({
        ("KIX", "SFO", 1, None): [_quote(620, "UA", stops=1, via="SFO")],
    })

    collect_once(provider, CHECK_DATE)

    row = _snapshot_rows()[0]
    assert row["check_date"] == CHECK_DATE.isoformat()
    assert row["departure_date"] == DEPARTURE
    assert (row["origin"], row["destination"]) == ("KIX", "SFO")
    assert row["stops"] == 1
    assert row["via_airport"] == "SFO"


def test_nonstop_via_airport_is_stored_as_empty_string_not_null():
    # NULL にすると SQLite の UNIQUE が NULL != NULL と評価して、直行便行の
    # 重複排除が黙って壊れる（既存 flight-price-monitor で判明済み）。
    _register("KIX", "SFO")
    provider = FakeProvider({("KIX", "SFO", 0, None): [_quote(700, "ANA")]})

    collect_once(provider, CHECK_DATE)
    collect_once(provider, CHECK_DATE)   # 同じ日に2回走らせる

    rows = _snapshot_rows()
    assert len(rows) == 1
    assert rows[0]["via_airport"] == ""


# ------------------------------------------------------------------
# 2.3 近接空港ルール
# ------------------------------------------------------------------

def test_san_route_also_collects_lax_but_never_lax_to_san():
    _register("HND", "SAN")
    provider = FakeProvider({
        ("HND", "SAN", 0, None): [_quote(900, "ANA")],
        ("HND", "LAX", 0, None): [_quote(490, "ZIPAIR")],
    })

    collect_once(provider, CHECK_DATE)

    # HND/NRT（東京）x SAN/LAX（南カリフォルニア）の組み合わせ。
    assert provider.routes_called == {
        ("HND", "SAN"), ("HND", "LAX"), ("NRT", "SAN"), ("NRT", "LAX")}
    # 近接空港どうしを結ぶ区間は収集しない（陸路で移動する前提）。
    assert ("LAX", "SAN") not in provider.routes_called
    assert ("HND", "NRT") not in provider.routes_called


def test_lax_origin_to_san_does_not_expand_to_itself():
    _register("LAX", "SAN")
    provider = FakeProvider()

    collect_once(provider, CHECK_DATE)

    assert provider.routes_called == {("LAX", "SAN")}


# ------------------------------------------------------------------
# 2.4 ルートの重複排除
# ------------------------------------------------------------------

def test_two_watches_on_the_same_route_are_collected_once():
    _register("KIX", "SFO")
    _register("KIX", "SFO")     # 別ユーザーが同じ条件を登録
    provider = FakeProvider({("KIX", "SFO", 0, None): [_quote(700, "ANA")]})

    collect_once(provider, CHECK_DATE)

    # API コストはユーザー数ではなく異なるルート数に比例する。
    assert provider.calls.count(("KIX", "SFO", 0, None)) == 1


def test_inactive_routes_are_not_queried():
    _register("KIX", "SFO")
    repository.upsert_monitored_route("NRT", "SFO", DEPARTURE)  # watch なし
    repository.deactivate_orphan_routes()

    provider = FakeProvider()
    collect_once(provider, CHECK_DATE)

    assert ("NRT", "SFO") not in provider.routes_called


# ------------------------------------------------------------------
# LCC 明示検索
# ------------------------------------------------------------------

def test_lcc_carriers_get_an_explicit_airlines_query():
    _register("KIX", "SFO")
    provider = FakeProvider()

    collect_once(provider, CHECK_DATE)

    # ZIPAIR は無指定検索だとほぼ結果に出ないので、明示指定の追加クエリが要る。
    assert ("KIX", "SFO", 0, ("ZG",)) in provider.calls


def test_lcc_result_survives_even_when_it_is_not_among_the_cheapest():
    _register("KIX", "SFO")
    provider = FakeProvider({
        ("KIX", "SFO", 0, None): [
            _quote(100 * i, f"Carrier{i}") for i in range(1, 7)
        ],
        ("KIX", "SFO", 0, ("ZG",)): [_quote(9999, "ZIPAIR")],
    })

    collect_once(provider, CHECK_DATE)

    airlines = {r["airline"] for r in _snapshot_rows()}
    # 検索ごとに独立して top_n を取るので、通常検索の安値に押し出されない。
    # 全部まとめてから切ると、取りこぼしを拾うための検索が無意味になる。
    assert "ZIPAIR" in airlines


def test_lcc_search_depth_is_configurable(monkeypatch):
    monkeypatch.setenv("LCC_SEARCH_MAX_STOPS", "1")
    get_settings.cache_clear()

    queries = plan_queries()
    lcc = [q for q in queries if q.airlines]
    assert {q.stops for q in lcc} == {0, 1}

    get_settings.cache_clear()


def test_query_plan_size_matches_the_documented_request_budget():
    # 既定: 通常検索 (stops 0,1) 2回 + LCC 1社 x 直行のみ 1回 = 3回/ルート。
    # 500ルート上限で 1500 リクエスト、平均3.5秒間隔で約90分。
    assert len(plan_queries()) == 3


# ------------------------------------------------------------------
# 失敗の扱い
# ------------------------------------------------------------------

def test_no_results_is_not_recorded_as_an_error():
    _register("KIX", "SFO")
    provider = FakeProvider()      # 全クエリが空を返す

    summary = collect_once(provider, CHECK_DATE)

    # 直行便が飛んでいない路線で fetch_errors を埋めない。
    assert _fetch_error_rows() == []
    assert summary["errors"] == 0
    assert summary["status"] == "success"


def test_a_failing_route_is_logged_and_the_others_still_collect():
    _register("KIX", "SFO")
    _register("NRT", "SEA")
    provider = FakeProvider(
        quotes={("NRT", "SEA", 0, None): [_quote(650, "UA")]},
        fail_routes=[("KIX", "SFO")],
    )

    summary = collect_once(provider, CHECK_DATE)

    errors = _fetch_error_rows()
    assert errors and all(e["origin"] == "KIX" for e in errors)
    # 1ルートの失敗で日次収集全体を落とさない。
    assert {(r["origin"], r["destination"]) for r in _snapshot_rows()} == {("NRT", "SEA")}
    assert summary["status"] == "partial"


def test_collect_once_never_raises_when_the_provider_explodes():
    _register("KIX", "SFO")

    class ExplodingProvider(PriceProvider):
        name = "exploding"

        def fetch(self, *_args, **_kwargs):
            raise ValueError("想定外の失敗")

    summary = collect_once(ExplodingProvider(), CHECK_DATE)

    assert summary["status"] == "failed"
    assert summary["rows_written"] == 0
    assert _fetch_error_rows()


def test_a_fully_failed_route_is_not_marked_collected():
    _register("KIX", "SFO")
    provider = FakeProvider(fail_routes=[("KIX", "SFO")])

    collect_once(provider, CHECK_DATE)

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT last_collected_at FROM monitored_routes"
        ).fetchall()
    assert all(r["last_collected_at"] is None for r in rows)


def test_a_successful_route_is_marked_collected():
    _register("KIX", "SFO")
    provider = FakeProvider({("KIX", "SFO", 0, None): [_quote(700, "ANA")]})

    collect_once(provider, CHECK_DATE)

    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_collected_at FROM monitored_routes"
        ).fetchone()
    assert row["last_collected_at"] is not None


# ------------------------------------------------------------------
# collection_logs
# ------------------------------------------------------------------

def test_the_run_is_recorded_in_collection_logs():
    _register("HND", "SAN")
    provider = FakeProvider({("HND", "SAN", 0, None): [_quote(900, "ANA")]})

    collect_once(provider, CHECK_DATE)

    log = repository.latest_collection_log()
    assert log["check_date"] == CHECK_DATE.isoformat()
    assert log["status"] == "success"
    assert log["source"] == "fake"
    assert log["routes_checked"] == 4      # 東京2空港 x 南カリフォルニア2空港
    assert log["records_saved"] == 1
    assert log["errors"] is None


def test_duplicate_itineraries_keep_the_cheapest_not_the_last():
    # 同じ航空会社・同じ経由地の便が出発時刻違いで複数返るのは実測で起きる。
    # snapshots の UNIQUE キーはそれを区別しないので、先に畳んでおかないと
    # 後勝ちの upsert で高い方が残る。
    _register("KIX", "SFO")
    provider = FakeProvider({
        ("KIX", "SFO", 1, None): [
            _quote(628, "Korean Air", stops=1, via="ICN"),
            _quote(890, "Korean Air", stops=1, via="ICN"),
        ],
    })

    collect_once(provider, CHECK_DATE)

    rows = _snapshot_rows()
    assert len(rows) == 1
    assert rows[0]["price_usd"] == 628.0


def test_duplicates_do_not_eat_the_top_n_budget():
    """**同じ便が2度来ても枠を食わない。** 畳んでから切る。

    ここでいう「同じ便」は社名・経由地・出発時刻まで一致するもの。
    時刻が違えば別の便なので畳まない（2026-09-08 以降）。
    """
    top_n = get_settings().top_n_itineraries
    _register("KIX", "SFO")
    provider = FakeProvider({
        ("KIX", "SFO", 0, None): (
            [_quote(100, "ANA"), _quote(150, "ANA")]           # 重複1組
            + [_quote(100 * i, f"Carrier{i}") for i in range(2, top_n + 4)]
        ),
    })

    collect_once(provider, CHECK_DATE)

    # 重複を畳んだ上で top_n 件。畳む前に切ると保存件数が top_n を下回る。
    assert len(_snapshot_rows()) == top_n


def test_the_cheapest_are_kept_when_a_search_returns_more_than_we_store():
    top_n = get_settings().top_n_itineraries
    _register("KIX", "SFO")
    provider = FakeProvider({
        ("KIX", "SFO", 0, None): [
            _quote(100 * i, f"Carrier{i}") for i in range(1, top_n + 4)
        ],
    })

    collect_once(provider, CHECK_DATE)

    rows = _snapshot_rows()
    assert len(rows) == top_n
    # 安い順に残る。
    assert max(r["price_usd"] for r in rows) == 100.0 * top_n


# ------------------------------------------------------------------
# 便の詳細の永続化
# ------------------------------------------------------------------

def test_flight_details_reach_the_database():
    """provider が持ち帰った詳細が snapshots に残ること。

    後から遡って取得できない項目なので、収集経路のどこかで落ちると
    その日のぶんが永久に欠ける。
    """
    _register("KIX", "SFO")
    provider = FakeProvider({
        ("KIX", "SFO", 0, None): [Quote(
            airline="ANA", stops=0, via_airport=None, price_usd=700.0,
            departure_time="2026-12-20T11:30", arrival_time="2026-12-20T18:45",
            air_minutes=555, plane_types="Boeing 787", carbon_g=812_000,
        )],
    })

    collect_once(provider, CHECK_DATE)

    row = _snapshot_rows()[0]
    assert row["departure_time"] == "2026-12-20T11:30"
    assert row["arrival_time"] == "2026-12-20T18:45"
    assert row["air_minutes"] == 555
    assert row["plane_types"] == "Boeing 787"
    assert row["carbon_g"] == 812_000


def test_two_flights_at_different_times_are_two_rows():
    """**同じ社・同じ経由地でも、時刻が違えば別の便。**

    2026-09-08 まで (経由回数, 経由地, 航空会社) の3項目で畳んでいたため、
    1日に何本も飛ぶ区間では安いほうだけが残っていた。SAN→HND の
    United+ANA・LAX 経由は 06:15 と 17:00 の2本あり、日によってどちらが
    安いかが入れ替わる。結果、1本の線に見えて**中身は別々の便のつぎはぎ**
    だった（8/23〜8/27 の $688 は、それ以前の $588 とは別の便）。
    """
    _register("KIX", "SFO")
    base = dict(airline="ANA", stops=0, via_airport=None)
    collect_once(FakeProvider({("KIX", "SFO", 0, None): [
        Quote(**base, price_usd=700.0, departure_time="2026-12-20T11:30"),
        Quote(**base, price_usd=880.0, departure_time="2026-12-20T09:05"),
    ]}), CHECK_DATE)

    rows = {r["departure_time"]: r["price_usd"] for r in _snapshot_rows()}
    assert rows == {"2026-12-20T11:30": 700.0, "2026-12-20T09:05": 880.0}


def test_recollecting_the_same_flight_updates_it_in_place():
    """同じ便（同じ時刻）を取り直したら、上書きであって増やさない。"""
    _register("KIX", "SFO")
    base = dict(airline="ANA", stops=0, via_airport=None,
                departure_time="2026-12-20T11:30")
    collect_once(FakeProvider({("KIX", "SFO", 0, None): [
        Quote(**base, price_usd=700.0)]}), CHECK_DATE)
    collect_once(FakeProvider({("KIX", "SFO", 0, None): [
        Quote(**base, price_usd=650.0)]}), CHECK_DATE)

    rows = _snapshot_rows()
    assert len(rows) == 1
    assert rows[0]["price_usd"] == 650.0


def test_a_provider_without_details_still_stores_the_price():
    _register("KIX", "SFO")
    provider = FakeProvider({("KIX", "SFO", 0, None): [_quote(700, "ANA")]})

    collect_once(provider, CHECK_DATE)

    row = _snapshot_rows()[0]
    assert row["price_usd"] == 700.0
    # **NULL ではなく ''。** UNIQUE キーの一部なので、NULL のままだと
    # SQLite が NULL != NULL と評価して重複排除が黙って壊れる。
    assert row["departure_time"] == ""


# ------------------------------------------------------------------
# 便が0件だった検索を残す（2026-08-30）
#
# **利用者の報告。** NRT-GMP（運航されていない区間）にモデル推定の価格が
# 出続けた。運航されていない区間は例外を出さず0件が返るだけなので、
# fetch_errors にも snapshots にも何も残らず、自動停止の判定にかからない。
# 「便が無い」ことを示す唯一の証拠なので、捨てずに残す。
# ------------------------------------------------------------------

def _empty_search_rows():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM empty_searches")]


def test_a_search_that_returns_nothing_is_recorded():
    _register("NRT", "GMP")
    collect_once(FakeProvider(), check_date=date.fromisoformat("2026-08-30"))

    rows = [r for r in _empty_search_rows()
            if (r["origin"], r["destination"]) == ("NRT", "GMP")]
    assert rows, "0件だった検索が残っていない"
    assert {r["check_date"] for r in rows} == {"2026-08-30"}


def test_an_empty_search_is_not_counted_as_an_error():
    """**fetch_errors に混ぜない。**

    混ぜると KPI の誤り件数と collection_status が「運航されていない区間が
    あるだけ」で悪化し、本当の不調が読めなくなる。
    """
    _register("NRT", "GMP")
    result = collect_once(FakeProvider(), check_date=date.today())

    assert _fetch_error_rows() == []
    assert result["status"] == "success", "0件は失敗ではない"


def test_a_route_that_returns_flights_records_no_empty_search():
    _register("NRT", "LAX")
    quotes = {("NRT", "LAX", s, None): [_quote(700, "ANA", stops=s)]
              for s in (0, 1)}
    collect_once(FakeProvider(quotes), check_date=date.today())

    assert [r for r in _empty_search_rows()
            if (r["origin"], r["destination"]) == ("NRT", "LAX")] == []


def test_a_failed_fetch_is_not_recorded_as_empty():
    """例外は「取れなかった」であって「便が無い」ではない。"""
    _register("NRT", "GMP")
    collect_once(FakeProvider(fail_routes=[("NRT", "GMP")]),
                 check_date=date.today())

    assert [r for r in _empty_search_rows()
            if (r["origin"], r["destination"]) == ("NRT", "GMP")] == []
    assert _fetch_error_rows()



# ── 調査用ルートの取得頻度（2026-10-14）──────────────────────────
#
# 出発が遠いほど価格は動かないので、調査用のモニターだけが参照するルートは
# 間を空けて引く。利用者のモニターが参照するルートは毎日引く。

from datetime import date as _date, timedelta as _td

from app.collect.collector import cadence_interval, collect_once, is_due

_CAD = ((60, 1), (150, 3), (100000, 7))


def test_the_interval_follows_days_before_departure():
    assert cadence_interval(0, _CAD) == 1
    assert cadence_interval(60, _CAD) == 1
    assert cadence_interval(61, _CAD) == 3
    assert cadence_interval(150, _CAD) == 3
    assert cadence_interval(151, _CAD) == 7
    assert cadence_interval(380, _CAD) == 7


def test_each_route_is_fetched_exactly_once_per_interval():
    """3日に1回なら、どの3日間にもちょうど1回。抜けも重複も無い。"""
    start = _date(2026, 10, 15)
    dep = (start + _td(days=120)).isoformat()
    for route_id in range(1, 20):
        hits = [is_due(route_id, dep, start + _td(days=k), _CAD) for k in range(3)]
        assert sum(hits) == 1, route_id


def test_load_is_spread_across_days():
    """同じ間隔のルートが同じ日に固まらない。"""
    day = _date(2026, 10, 15)
    dep = (day + _td(days=200)).isoformat()             # 週1回の帯
    due = sum(is_due(rid, dep, day, _CAD) for rid in range(1, 701))
    assert 90 <= due <= 110                              # 700/7 = 100


def _research(origin, destination, departure_date):
    rid = repository.upsert_monitored_route(origin, destination, departure_date)
    repository.create_watch(origin, destination, departure_date, [rid], purpose="research")
    return rid


def _queried(provider):
    return {(o, d) for (o, d, *_rest) in provider.calls}


def test_far_research_routes_are_skipped_but_user_routes_are_not(monkeypatch):
    today = _date(2026, 10, 15)
    far = (today + _td(days=200)).isoformat()
    near = (today + _td(days=30)).isoformat()
    _research("SAN", "NRT", far)            # 調査用・遠い → 週1回
    _research("NRT", "SAN", near)           # 調査用・近い → 毎日
    _register("KIX", "SFO", far)            # 利用者・遠い → 毎日
    seen = {}
    for k in range(7):
        provider = FakeProvider()
        collect_once(provider, check_date=today + _td(days=k))
        for key in _queried(provider):
            seen[key] = seen.get(key, 0) + 1
    assert seen[("SAN", "NRT")] == 1
    assert seen[("NRT", "SAN")] == 7
    assert seen[("KIX", "SFO")] == 7


def test_a_route_shared_with_a_user_watch_stays_daily():
    today = _date(2026, 10, 15)
    far = (today + _td(days=200)).isoformat()
    rid = _research("SAN", "HND", far)
    repository.create_watch("SAN", "HND", far, [rid])     # 利用者も同じルートを見ている
    assert rid not in repository.research_only_route_ids(today.isoformat())


def test_cadence_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("RESEARCH_CADENCE_ENABLED", "false")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        today = _date(2026, 10, 15)
        _research("SAN", "NRT", (today + _td(days=200)).isoformat())
        result = collect_once(FakeProvider(), check_date=today)
        assert result["skipped_by_cadence"] == 0
    finally:
        get_settings.cache_clear()
