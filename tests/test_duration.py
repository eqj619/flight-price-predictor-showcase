"""総所要時間（飛行 + 乗継）の収集と絞り込みのテスト。

きっかけは実測。NRT→SAN / 2026-12-20 の最安は $799 だったが、
総所要 33h10（うちカルガリーでの乗継 20h35）で、買い時判定の入力に
なっていた。「最安」が実際には選ばれない便では、判定そのものが無意味になる。
"""
from datetime import date, datetime, timedelta

import pytest

from app.collect import fast_flights_provider as ffp
from app.config import get_settings
from app.db import repository
from app.predict.engine import build_trend
from app.routing import expand_routes


TODAY = date(2026, 8, 10)
DEPARTURE = "2026-12-20"


# ----------------------------------------------------------------------
# 収集: 総所要の計算
# ----------------------------------------------------------------------

class _Stamp:
    def __init__(self, dt): self.date = (dt.year, dt.month, dt.day); self.time = (dt.hour, dt.minute)


class _Seg:
    def __init__(self, frm, to, dep, arr, duration, plane="Boeing 787"):
        self.from_airport = type("A", (), {"code": frm})()
        self.to_airport = type("A", (), {"code": to})()
        self.departure = _Stamp(dep)
        self.arrival = _Stamp(arr)
        self.duration = duration
        self.plane_type = plane


class _Group:
    def __init__(self, segments): self.flights = segments; self.carbon = None


def _dt(s):
    return datetime.fromisoformat(s)


def test_nonstop_total_equals_air_time():
    g = _Group([_Seg("NRT", "SAN", _dt("2026-12-20T17:10"), _dt("2026-12-20T09:45"), 575)])
    d = ffp._flight_details(g)
    assert d["air_minutes"] == 575
    assert d["total_minutes"] == 575     # 乗継なし


def test_one_stop_total_includes_layover():
    """実測の NRT-YYC-SAN と同じ形。飛行 12h35 + 乗継 20h35 = 33h10。"""
    g = _Group([
        _Seg("NRT", "YYC", _dt("2026-12-20T18:35"), _dt("2026-12-20T12:45"), 550),
        _Seg("YYC", "SAN", _dt("2026-12-21T09:20"), _dt("2026-12-21T10:45"), 205),
    ])
    d = ffp._flight_details(g)
    assert d["air_minutes"] == 755              # 12h35
    assert d["total_minutes"] == 755 + 1235     # + 乗継 20h35 = 33h10
    assert d["total_minutes"] == 1990


def test_total_is_not_arrival_minus_departure():
    """**到着 − 出発では出せない。** 現地時刻なので日付変更線で逆転する。"""
    g = _Group([_Seg("NRT", "SAN", _dt("2026-12-20T17:10"), _dt("2026-12-20T09:45"), 575)])
    d = ffp._flight_details(g)
    naive = (_dt(d["arrival_time"]) - _dt(d["departure_time"])).total_seconds() / 60
    assert naive < 0                    # 素の引き算は負になる
    assert d["total_minutes"] == 575    # 正しい値は区間から積み上げる


def test_backwards_layover_is_unknown_not_zero():
    """乗継が負になる（別空港・パースずれ）ときは推測せず None。"""
    g = _Group([
        _Seg("NRT", "LAX", _dt("2026-12-20T17:00"), _dt("2026-12-20T11:00"), 600),
        _Seg("LAX", "SAN", _dt("2026-12-20T09:00"), _dt("2026-12-20T10:00"), 60),
    ])
    d = ffp._flight_details(g)
    assert d["air_minutes"] == 660
    assert d["total_minutes"] is None


def test_partial_segment_durations_give_no_total():
    """区間の飛行時間が1つでも欠けたら、合計も総所要も出さない。"""
    segs = [_Seg("NRT", "YYC", _dt("2026-12-20T18:35"), _dt("2026-12-20T12:45"), 550),
            _Seg("YYC", "SAN", _dt("2026-12-21T09:20"), _dt("2026-12-21T10:45"), None)]
    d = ffp._flight_details(_Group(segs))
    assert d["air_minutes"] is None
    assert d["total_minutes"] is None


# ----------------------------------------------------------------------
# 絞り込み
# ----------------------------------------------------------------------

def _snap(check_date, airline, price, total_minutes, stops=1, via="YYC"):
    return repository.Snapshot(
        check_date=check_date, departure_date=DEPARTURE,
        origin="NRT", destination="SAN", stops=stops, via_airport=via,
        airline=airline, price_usd=price, source="real",
        fetched_at=f"{check_date}T03:00:00",
        air_minutes=total_minutes, total_minutes=total_minutes,
    )


@pytest.fixture
def three_itineraries():
    """実測に近い並び。最安が最長という状況を作る。"""
    rows = []
    for i in range(3):
        d = (date(2026, 8, 8) + timedelta(days=i)).isoformat()
        rows += [
            _snap(d, "Air Canada", 799, 1990),                    # 33h10
            _snap(d, "American", 1109, 1043, via="DFW"),          # 17h23
            _snap(d, "ANA", 2473, 575, stops=0, via=None),        # 9h35 直行
        ]
    repository.upsert_snapshots(rows)


def test_long_itinerary_is_excluded(three_itineraries):
    all_trends = build_trend("NRT", "SAN", DEPARTURE)
    assert len(all_trends) == 3

    capped = build_trend("NRT", "SAN", DEPARTURE, max_total_minutes=24 * 60)
    airlines = {t.airline for t in capped.values()}
    assert "Air Canada" not in airlines       # 33h10 は外れる
    assert airlines == {"American", "ANA"}


def test_no_limit_keeps_everything(three_itineraries):
    for limit in (None, 0, -1):
        trends = build_trend("NRT", "SAN", DEPARTURE, max_total_minutes=limit)
        assert len(trends) == 3, f"limit={limit}"


def test_unknown_duration_is_kept(three_itineraries):
    """列を足す前の実測（total_minutes = NULL）を「長すぎる」扱いにしない。

    落とすと、較正の入力から過去の実測がまるごと消える。
    """
    repository.upsert_snapshots([
        repository.Snapshot(
            check_date="2026-08-10", departure_date=DEPARTURE,
            origin="NRT", destination="SAN", stops=1, via_airport="ICN",
            airline="Korean Air", price_usd=628.0, source="real",
            fetched_at="2026-08-10T03:00:00",
        )
    ])
    trends = build_trend("NRT", "SAN", DEPARTURE, max_total_minutes=24 * 60)
    assert any(t.airline == "Korean Air" for t in trends.values())
    assert next(t for t in trends.values() if t.airline == "Korean Air").total_minutes is None


def test_trend_reports_duration(three_itineraries):
    trends = build_trend("NRT", "SAN", DEPARTURE)
    by_airline = {t.airline: t.total_minutes for t in trends.values()}
    assert by_airline == {"Air Canada": 1990, "American": 1043, "ANA": 575}


def test_excluded_count_uses_latest_check_date_only(three_itineraries):
    """過去に一度出ただけの便を数え続けない。"""
    assert repository.count_long_itineraries("NRT", "SAN", DEPARTURE, 24 * 60) == 1
    assert repository.count_long_itineraries("NRT", "SAN", DEPARTURE, 40 * 60) == 0
    assert repository.count_long_itineraries("NRT", "SAN", DEPARTURE, 0) == 0


def test_recommendation_ignores_the_long_cheap_flight(three_itineraries):
    """買い時判定の入力からも外れる（表示だけの絞り込みではない）。"""
    from app.predict.engine import recommend

    unlimited = recommend("NRT", "SAN", DEPARTURE, today=TODAY)
    capped = recommend("NRT", "SAN", DEPARTURE, today=TODAY, max_total_minutes=24 * 60)
    assert unlimited["current_best"]["price_usd"] == 799.0
    assert capped["current_best"]["price_usd"] == 1109.0


# ----------------------------------------------------------------------
# モニターの設定
# ----------------------------------------------------------------------

def _watch(**kw):
    route_ids = [repository.upsert_monitored_route(o, d, DEPARTURE)
                 for o, d in expand_routes("NRT", "SAN")]
    return repository.create_watch("NRT", "SAN", DEPARTURE, route_ids, **kw)


def test_new_watch_gets_the_default_cap():
    w = _watch()
    assert w.max_total_minutes == get_settings().max_total_minutes_default
    assert repository.get_watch_by_token(w.token).max_total_minutes == 24 * 60


def test_cap_can_be_removed():
    w = _watch()
    assert repository.update_max_total_minutes(w.token, None)
    assert repository.get_watch_by_token(w.token).max_total_minutes is None


def test_explicit_none_at_creation_is_not_overwritten_by_default():
    """None は「上限なし」。既定値で塗り潰さない。"""
    w = _watch(max_total_minutes=None)
    assert w.max_total_minutes is None
    assert repository.get_watch_by_token(w.token).max_total_minutes is None


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------

def test_api_exposes_cap_and_excluded_count(three_itineraries):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    created = client.post("/api/watches", json={
        "origin": "NRT", "destination": "SAN", "departure_date": DEPARTURE,
    })
    assert created.status_code == 201, created.text
    token = created.json()["token"]

    body = client.get(f"/api/watches/{token}").json()
    assert body["max_total_minutes"] == 24 * 60
    main = next(r for r in body["routes"]
                if (r["origin"], r["destination"]) == ("NRT", "SAN"))
    assert main["excluded_by_duration"] == 1
    assert all(s["airline"] != "Air Canada" for s in main["series"])

    # 0 は「上限なし」。None（未指定）は「変更しない」。
    assert client.patch(f"/api/watches/{token}",
                        json={"max_total_minutes": 0}).status_code == 204
    body = client.get(f"/api/watches/{token}").json()
    assert body["max_total_minutes"] is None
    main = next(r for r in body["routes"]
                if (r["origin"], r["destination"]) == ("NRT", "SAN"))
    assert main["excluded_by_duration"] == 0
    assert any(s["airline"] == "Air Canada" for s in main["series"])


def test_patch_without_the_field_keeps_the_cap():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    token = _watch().token
    assert client.patch(f"/api/watches/{token}",
                        json={"airline_filter": ["ANA"]}).status_code == 204
    assert repository.get_watch_by_token(token).max_total_minutes == 24 * 60


# ----------------------------------------------------------------------
# 埋め戻し
# ----------------------------------------------------------------------

def test_nonstop_backfill_is_exact_and_leaves_connections_alone():
    """直行便は乗継が無いので 総所要 = 飛行時間 で確定できる。1経由は埋めない。"""
    from app.db.connection import _backfill_nonstop_duration, get_connection

    repository.upsert_snapshots([
        repository.Snapshot(
            check_date="2026-08-01", departure_date=DEPARTURE, origin="NRT",
            destination="SAN", stops=0, via_airport=None, airline="ANA",
            price_usd=900.0, source="real", fetched_at="x", air_minutes=575),
        repository.Snapshot(
            check_date="2026-08-01", departure_date=DEPARTURE, origin="NRT",
            destination="SAN", stops=1, via_airport="YYC", airline="Air Canada",
            price_usd=799.0, source="real", fetched_at="x", air_minutes=755),
    ])
    with get_connection() as conn:
        assert _backfill_nonstop_duration(conn) == 1
        assert _backfill_nonstop_duration(conn) == 0     # 冪等

    rows = {r["airline"]: r["total_minutes"]
            for r in repository.history_for("NRT", "SAN", DEPARTURE)}
    assert rows["ANA"] == 575
    assert rows["Air Canada"] is None
