"""往復対応・段階E — API。

**主題は「往復に判定を出さないこと」。** 曲線は片道 212系列から推定した
もので、往復では遠い側の平坦さしか確かめられていない。谷と遅延の罰は未測定
（docs/round-trip-findings-2026-08-19.md）。判定の中身はその2つで決まるので、
流用すれば課題2（未検証のモデルを判定に使う）を繰り返すことになる。
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import repository as repo
from app.main import app

client = TestClient(app)
TODAY = date.today()
DEP = (TODAY + timedelta(days=120)).isoformat()
RET = (TODAY + timedelta(days=130)).isoformat()


def _create(**kw):
    body = {"origin": "NRT", "destination": "LAX", "departure_date": DEP}
    body.update(kw)
    return client.post("/api/watches", json=body)


def _seed(token, price, return_date=""):
    """そのモニターのルートに実測を1点入れる。"""
    routes = repo.routes_for_watch(token)
    r = routes[0]
    repo.upsert_snapshots([repo.Snapshot(
        check_date=TODAY.isoformat(), departure_date=r.departure_date,
        origin=r.origin, destination=r.destination, stops=0, via_airport=None,
        airline="ANA", price_usd=price, source="real",
        fetched_at="x", return_date=return_date)])


# ============================================================
# 作成
# ============================================================

def test_a_round_trip_monitor_can_be_created():
    res = _create(return_date=RET)
    assert res.status_code == 201
    assert res.json()["return_date"] == RET


def test_a_one_way_monitor_still_works_without_the_field():
    """**既存の呼び出しを壊さない。** return_date を送らなければ片道。"""
    res = _create()
    assert res.status_code == 201
    assert res.json()["return_date"] is None
    assert repo.routes_for_watch(res.json()["token"])[0].return_date == ""


@pytest.mark.parametrize("bad", [
    "2020-01-01",                                   # 出発日より前
])
def test_a_return_before_departure_is_rejected(bad):
    assert _create(return_date=bad).status_code == 422


def test_a_return_on_the_departure_day_is_rejected():
    assert _create(return_date=DEP).status_code == 422


def test_a_round_trip_consumes_one_route_like_a_one_way():
    """容量計画の前提。往復1件が食う枠は片道1件と同じ。"""
    a = _create(return_date=RET).json()["token"]
    b = _create(origin="SFO").json()["token"]
    assert len(repo.routes_for_watch(a)) == len(repo.routes_for_watch(b)) == 1


# ============================================================
# 表示 — ここが本題
# ============================================================

def test_a_round_trip_shows_observed_prices_but_no_verdict():
    token = _create(return_date=RET).json()["token"]
    _seed(token, 1580.0, RET)

    rec = client.get(f"/api/watches/{token}").json()["routes"][0]["recommendation"]

    # 実測は出す（これは観測そのもの）。
    assert rec["current_best"]["price_usd"] == 1580.0
    assert rec["basis"] == "round_trip_observed"

    # 曲線から出るものは全部落とす。
    assert rec["verdict"] is None, "往復に買い時判定を出してはいけない"
    assert rec["predicted_min"] is None, "往復に予測値を出してはいけない"
    assert rec["expected_low_window"] is None, "往復に底値窓を出してはいけない"
    assert rec["price_rises_after"] is None, "往復に値上がり日を出してはいけない"


def test_a_round_trip_draws_no_projection_line():
    """グラフの予測線も曲線由来なので引かない。"""
    token = _create(return_date=RET).json()["token"]
    _seed(token, 1580.0, RET)

    series = client.get(f"/api/watches/{token}").json()["routes"][0]["series"]
    assert series, "実測が出ていない"
    assert all(not s["projected"] for s in series), "往復に予測線を引いている"
    assert all(s["observed"] for s in series)


def test_a_one_way_still_gets_its_verdict():
    """**片道の機能を落としていないこと。**"""
    token = _create().json()["token"]
    for i in range(6):
        d = (TODAY - timedelta(days=i)).isoformat()
        r = repo.routes_for_watch(token)[0]
        repo.upsert_snapshots([repo.Snapshot(
            check_date=d, departure_date=r.departure_date,
            origin=r.origin, destination=r.destination, stops=0,
            via_airport=None, airline="ANA", price_usd=600.0 + i,
            source="real", fetched_at="x")])

    rec = client.get(f"/api/watches/{token}").json()["routes"][0]["recommendation"]
    assert rec["verdict"] is not None
    assert rec["basis"] == "observed"
    assert rec["price_rises_after"] is not None


def test_a_round_trip_without_observations_shows_nothing_invented():
    """**推定で埋めない。** 実測が無ければ空。曲線で埋めると根拠が無い。"""
    token = _create(return_date=RET).json()["token"]
    body = client.get(f"/api/watches/{token}").json()
    assert body["routes"][0]["series"] == []
    assert body["routes"][0]["recommendation"] is None


def test_the_watch_payload_exposes_the_return_date():
    token = _create(return_date=RET).json()["token"]
    assert client.get(f"/api/watches/{token}").json()["return_date"] == RET

    token = _create().json()["token"]
    assert client.get(f"/api/watches/{token}").json()["return_date"] == ""


def test_one_way_and_round_trip_on_the_same_leg_do_not_mix():
    """同じ区間・同じ出発日で両方作っても、価格が混ざらない。"""
    ow = _create().json()["token"]
    rt = _create(return_date=RET).json()["token"]
    _seed(ow, 620.0)
    _seed(rt, 1580.0, RET)

    a = client.get(f"/api/watches/{ow}").json()["routes"][0]["recommendation"]
    b = client.get(f"/api/watches/{rt}").json()["routes"][0]["recommendation"]
    assert a["current_best"]["price_usd"] == 620.0
    assert b["current_best"]["price_usd"] == 1580.0


# ============================================================
# 観測がまだ無い往復に、嘘をつかないこと
# ============================================================

def test_a_round_trip_without_data_is_not_labelled_as_a_model_estimate():
    """**「モデルによる推定です」と言ってはいけない。**

    往復では推定を一切出していない（実測が無ければ空で返す）。それなのに
    片道と同じ basis='simulated' を返していたため、画面が
    「表示はすべてモデルによる推定です」と出しながら中身は空、という
    状態になっていた（2026-08-20 に利用者から指摘を受けて発覚）。
    """
    token = _create(return_date=RET).json()["token"]
    body = client.get(f"/api/watches/{token}").json()
    summary = body["routes"][0]["summary"]

    assert summary["basis"] == "round_trip_pending"
    assert summary["basis"] != "simulated", "推定を出していないのに推定と名乗っている"
    assert summary["best"] is None
    assert body["routes"][0]["series"] == []


def test_a_one_way_without_data_shows_no_price_either():
    """**片道も往復も、実測が無ければ値段を出さない**（2026-08-30）。

    往復は前から推定に落とさない設計だった。片道だけ推定を出していたが、
    「この区間に便があるか確かめていない」点は片道でも同じなので揃えた。
    """
    token = _create().json()["token"]
    summary = client.get(f"/api/watches/{token}").json()["routes"][0]["summary"]
    assert summary["basis"] in ("unconfirmed", "transferred")


def test_a_round_trip_with_data_is_not_pending():
    token = _create(return_date=RET).json()["token"]
    _seed(token, 1580.0, RET)
    summary = client.get(f"/api/watches/{token}").json()["routes"][0]["summary"]
    assert summary["basis"] == "observed"
    assert summary["best"]["price_usd"] == 1580.0


# ============================================================
# 取得元の履歴をグラフに出す
# ============================================================

def test_the_source_history_is_not_exposed_on_the_route_payload():
    """**グラフから外した**（2026-08-24、利用者の指摘）。

    グラフは1便を固定して追う作りなのに、この線は便を特定しない
    「その検索での最安値」だった。**同じ絵に別の量を重ねると混乱する。**
    画面で使わない以上、ルートごとに問い合わせて返すのもやめる。

    保存そのものは続ける（曲線の推定に使う）。
    """
    token = _create(return_date=RET).json()["token"]
    route = client.get(f"/api/watches/{token}").json()["routes"][0]
    assert "price_history" not in route


def test_the_source_history_is_kept_out_of_the_observed_summary():
    """**履歴を実測として数えない。** 別の量なので混ぜてはいけない。"""
    token = _create(return_date=RET).json()["token"]
    r = repo.routes_for_watch(token)[0]
    repo.save_price_history(r.origin, r.destination, r.departure_date,
                            [("2026-07-01", 99.0)], return_date=RET)

    route = client.get(f"/api/watches/{token}").json()["routes"][0]
    # 履歴に $99 があっても、実測ゼロなら「最安 $99」とは言わない。
    assert route["summary"]["basis"] == "round_trip_pending"
    assert route["summary"]["best"] is None
    assert route["series"] == []
    assert route["recommendation"] is None


def test_one_way_and_round_trip_histories_stay_separate_in_storage():
    """画面から外しても、**保存側で混ざってはいけない。**

    曲線の推定にはこの表を使う。片道と往復が混ざると、金額が倍違うものを
    同じ系列として推定することになる。
    """
    ow = _create().json()["token"]
    r = repo.routes_for_watch(ow)[0]
    repo.save_price_history(r.origin, r.destination, r.departure_date,
                            [("2026-07-01", 620.0)])
    repo.save_price_history(r.origin, r.destination, r.departure_date,
                            [("2026-07-01", 1580.0)], return_date=RET)

    assert [p["price_usd"] for p in
            repo.price_history(r.origin, r.destination, r.departure_date)] == [620.0]
    assert [p["price_usd"] for p in
            repo.price_history(r.origin, r.destination, r.departure_date,
                               RET)] == [1580.0]
