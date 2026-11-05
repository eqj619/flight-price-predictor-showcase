"""API と匿名マルチテナントのテスト。

認証が無い代わりに、トークンが capability として成立していること
（推測できない / 漏れない / 知っていれば誰でも開ける）を固定する。
"""
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import repository
from app.main import app

DEPARTURE = (date.today() + timedelta(days=60)).isoformat()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _create(client, origin="NRT", destination="SAN", departure=None, **extra):
    payload = {
        "origin": origin,
        "destination": destination,
        "departure_date": departure or DEPARTURE,
        **extra,
    }
    return client.post("/api/watches", json=payload)


def _seed_price(origin, destination, departure, price, check_date=None,
                airline="ANA", source="real"):
    check_date = check_date or date.today()
    repository.upsert_snapshots([repository.Snapshot(
        check_date=check_date.isoformat(), departure_date=departure,
        origin=origin, destination=destination, stops=0, via_airport=None,
        airline=airline, price_usd=price, source=source,
        fetched_at=check_date.isoformat(),
    )])


# ======================================================================
# 登録
# ======================================================================

def test_creating_a_watch_returns_a_share_url(client):
    response = _create(client)
    assert response.status_code == 201

    body = response.json()
    assert body["url"] == f"/w/{body['token']}"
    assert body["origin"] == "NRT"


def test_tokens_are_unguessable_and_unique(client):
    tokens = {_create(client).json()["token"] for _ in range(5)}
    assert len(tokens) == 5
    # 連番 ID は不可。128bit を URL-safe base64 にすると22文字。
    assert all(len(t) >= 20 for t in tokens)
    assert not any(t.isdigit() for t in tokens)


def test_airport_codes_are_normalized(client):
    body = _create(client, origin=" nrt ", destination="san").json()
    assert (body["origin"], body["destination"]) == ("NRT", "SAN")


def test_the_nearby_rule_is_applied_at_registration(client):
    # 既定はオフ（2026-08-14 変更）。ここで見たいのは規則そのものなので明示的に入れる。
    routes = _create(client, destination="SAN",
                     nearby_airports=True).json()["routes"]

    # 東京2空港 x 南カリフォルニア2空港。要求されたルートだけが nearby=False。
    assert {(r["origin"], r["destination"]) for r in routes} == {
        ("NRT", "SAN"), ("NRT", "LAX"), ("HND", "SAN"), ("HND", "LAX")}
    assert [r for r in routes if not r["nearby"]] == [
        {"origin": "NRT", "destination": "SAN", "nearby": False}]


def test_the_nearby_rule_can_be_disabled_at_registration(client):
    routes = _create(client, destination="SAN",
                     nearby_airports=False).json()["routes"]
    assert [(r["origin"], r["destination"]) for r in routes] == [("NRT", "SAN")]


def test_the_nearby_rule_can_be_toggled_afterwards(client):
    token = _create(client, destination="SAN", nearby_airports=True).json()["token"]
    assert len(client.get(f"/api/watches/{token}").json()["routes"]) == 4

    # 切る -> 要求どおりの1ルートだけになり、他は収集も止まる
    assert client.patch(f"/api/watches/{token}",
                        json={"nearby_airports": False}).status_code == 204
    body = client.get(f"/api/watches/{token}").json()
    assert body["nearby_airports"] is False
    assert [(r["origin"], r["destination"]) for r in body["routes"]] == [("NRT", "SAN")]
    assert repository.count_active_routes() == 1

    # 戻す -> 元どおり
    client.patch(f"/api/watches/{token}", json={"nearby_airports": True})
    body = client.get(f"/api/watches/{token}").json()
    assert body["nearby_airports"] is True
    assert len(body["routes"]) == 4


def test_toggling_the_nearby_rule_keeps_the_price_history(client):
    token = _create(client, destination="SAN").json()["token"]
    _seed_price("NRT", "LAX", DEPARTURE, 494.0, airline="ZIPAIR Tokyo")

    client.patch(f"/api/watches/{token}", json={"nearby_airports": False})
    client.patch(f"/api/watches/{token}", json={"nearby_airports": True})

    # 切っている間も snapshots は消さない。戻したら初日から履歴が見える。
    assert repository.history_for("NRT", "LAX", DEPARTURE)


def test_a_partial_patch_leaves_other_settings_alone(client):
    token = _create(client, destination="LAX",
                    airline_filter=["ANA"]).json()["token"]

    client.patch(f"/api/watches/{token}", json={"nearby_airports": False})

    # 航空会社だけ指定しなかったからといって全社に戻してはいけない。
    assert client.get(f"/api/watches/{token}").json()["airline_filter"] == ["ANA"]


@pytest.mark.parametrize("payload", [
    {"origin": "NARITA"},                 # 3文字でない
    {"origin": "N1T"},                    # 英字でない
    {"destination": "NRT"},               # 出発と到着が同じ
    {"departure": "2020-01-01"},          # 過去
    {"departure": "not-a-date"},
])
def test_invalid_input_is_rejected(client, payload):
    departure = payload.pop("departure", None)
    assert _create(client, departure=departure, **payload).status_code == 422


def test_a_departure_date_too_far_out_is_rejected(client):
    far = (date.today() + timedelta(days=3650)).isoformat()
    # 無制限にすると「10年先」の収集し続けるだけのモニターを作られる。
    assert _create(client, departure=far).status_code == 422


def test_a_malformed_email_is_rejected(client):
    assert _create(client, notify_email="nope").status_code == 422


# ======================================================================
# 取得
# ======================================================================

def test_a_watch_can_be_opened_with_only_its_token(client):
    token = _create(client).json()["token"]
    # 別のブラウザ = Cookie もヘッダも引き継がない。トークンだけで開ける。
    with TestClient(app) as other_browser:
        assert other_browser.get(f"/api/watches/{token}").status_code == 200


def test_an_unknown_token_is_not_found(client):
    assert client.get("/api/watches/definitely-not-a-real-token").status_code == 404


def test_the_watch_payload_carries_both_routes_of_the_san_rule(client):
    token = _create(client, destination="SAN", nearby_airports=True).json()["token"]
    _seed_price("NRT", "SAN", DEPARTURE, 1041.0)
    _seed_price("NRT", "LAX", DEPARTURE, 494.0)

    body = client.get(f"/api/watches/{token}").json()

    by_destination = {r["destination"]: r for r in body["routes"]}
    assert set(by_destination) == {"SAN", "LAX"}
    assert by_destination["LAX"]["nearby"] is True
    # 近接空港のほうが安いという、このルールの存在意義そのものが見えること。
    assert (by_destination["LAX"]["recommendation"]["current_best"]["price_usd"]
            < by_destination["SAN"]["recommendation"]["current_best"]["price_usd"])


def test_a_watch_without_prices_yet_shows_no_price_at_all(client):
    """**収集前は値段を出さない**（2026-08-30 の指摘で反転）。

    以前はモデル推定を出していた。しかし実測が1件も無い状態は
    「この出発日の値段を知らない」ではなく **「この区間に便があるかも
    確かめていない」** という意味で、そこに値段が付いていること自体が
    「この路線はある」という主張になる。運航されていない区間
    （NRT-GMP）だと分かるのは3日後で、それまで嘘をつき続けていた。
    """
    token = _create(client).json()["token"]
    body = client.get(f"/api/watches/{token}").json()

    for route in body["routes"]:
        assert route["summary"]["basis"] == "unconfirmed"
        assert route["summary"]["best"] is None
        assert route["summary"]["avg_price_usd"] is None
        assert route["recommendation"] is None, "買い時判定も出さない"
        assert route["series"] == []


def test_an_unconfirmed_route_offers_nothing_to_name_an_airline_with(client):
    """実測が無い区間では系列ごと出さないので、社名も出ようがない。

    以前は社名を空にしたモデル推定を出していた。**値段を出さないなら
    社名の問題も起きない。** 飛んでいない社が飛んでいるように見える
    余地を、表示ごと無くした。
    """
    token = _create(client).json()["token"]
    body = client.get(f"/api/watches/{token}").json()

    assert all(r["series"] == [] for r in body["routes"])


def test_another_departure_date_lets_the_estimate_name_airlines(client):
    other = (date.today() + timedelta(days=120)).isoformat()
    _seed_price("NRT", "LAX", other, 494.0, airline="ZIPAIR Tokyo")
    _seed_price("NRT", "LAX", other, 2200.0, airline="ANA")

    token = _create(client, destination="LAX").json()["token"]
    body = client.get(f"/api/watches/{token}").json()
    route = next(r for r in body["routes"]
                 if (r["origin"], r["destination"]) == ("NRT", "LAX"))

    assert route["summary"]["basis"] == "transferred"
    # このルートで実際に観測された社だけが名乗る。
    assert {s["airline"] for s in route["series"]} == {"ZIPAIR Tokyo", "ANA"}
    # 便ごとに較正するので、社ごとの水準差が残る。
    prices = {s["airline"]: s["projected"][0]["price_usd"] for s in route["series"]}
    assert prices["ZIPAIR Tokyo"] < prices["ANA"]
    # それでも3値判定は出さない（この出発日の実売価格が無いため）。
    assert route["recommendation"]["verdict"] == "insufficient_data"


def test_airlines_seen_on_another_route_are_not_borrowed(client):
    other = (date.today() + timedelta(days=120)).isoformat()
    _seed_price("NRT", "LAX", other, 494.0, airline="ZIPAIR Tokyo")

    # ZIPAIR は NRT-LAX でしか観測されていない。SAN 側で名乗らせない。
    token = _create(client, destination="SAN").json()["token"]
    body = client.get(f"/api/watches/{token}").json()
    san = next(r for r in body["routes"] if r["destination"] == "SAN")

    # SAN 側は実測が無いので未確認。系列ごと出さない
    # （＝ ZIPAIR が SAN 側で名乗る余地が無い）。
    assert san["summary"]["basis"] == "unconfirmed"
    assert san["series"] == []


def test_the_summary_marks_an_unconfirmed_route_as_such(client):
    token = _create(client).json()["token"]
    body = client.get(f"/api/watches/{token}").json()

    summary = body["routes"][0]["summary"]
    assert summary["basis"] == "unconfirmed"
    assert summary["records"] == 0
    assert summary["days_monitored"] == 0


def test_the_summary_uses_only_real_prices_once_they_exist(client):
    token = _create(client, destination="LAX").json()["token"]
    _seed_price("NRT", "LAX", DEPARTURE, 700.0, airline="ANA")

    body = client.get(f"/api/watches/{token}").json()
    summary = next(r for r in body["routes"]
                   if (r["origin"], r["destination"]) == ("NRT", "LAX"))["summary"]

    assert summary["basis"] == "observed"
    assert summary["best"]["price_usd"] == 700.0
    assert summary["records"] == 1


def test_the_airline_filter_can_be_changed_after_creation(client):
    token = _create(client, destination="LAX").json()["token"]
    _seed_price("NRT", "LAX", DEPARTURE, 2200.0, airline="ANA")
    _seed_price("NRT", "LAX", DEPARTURE, 494.0, airline="ZIPAIR Tokyo")

    assert client.patch(f"/api/watches/{token}",
                        json={"airline_filter": ["ZIPAIR Tokyo"]}).status_code == 204

    body = client.get(f"/api/watches/{token}").json()
    assert body["airline_filter"] == ["ZIPAIR Tokyo"]
    assert {s["airline"] for r in body["routes"] for s in r["series"]} == {"ZIPAIR Tokyo"}


def test_several_airlines_can_be_selected(client):
    token = _create(client, destination="LAX").json()["token"]
    for name, price in [("ANA", 2200.0), ("ZIPAIR Tokyo", 494.0), ("JAL", 2300.0)]:
        _seed_price("NRT", "LAX", DEPARTURE, price, airline=name)

    client.patch(f"/api/watches/{token}",
                 json={"airline_filter": ["ZIPAIR Tokyo", "ANA"]})
    body = client.get(f"/api/watches/{token}").json()

    assert {s["airline"] for r in body["routes"] for s in r["series"]} == {
        "ZIPAIR Tokyo", "ANA"}


def test_an_empty_filter_means_every_airline(client):
    token = _create(client, destination="LAX",
                    airline_filter=["ANA"]).json()["token"]
    _seed_price("NRT", "LAX", DEPARTURE, 2200.0, airline="ANA")
    _seed_price("NRT", "LAX", DEPARTURE, 494.0, airline="ZIPAIR Tokyo")

    client.patch(f"/api/watches/{token}", json={"airline_filter": []})
    body = client.get(f"/api/watches/{token}").json()
    route = next(r for r in body["routes"]
                 if (r["origin"], r["destination"]) == ("NRT", "LAX"))

    assert {s["airline"] for s in route["series"]} == {"ANA", "ZIPAIR Tokyo"}


def test_the_filter_choices_come_from_observed_airlines(client):
    token = _create(client, destination="LAX").json()["token"]
    _seed_price("NRT", "LAX", DEPARTURE, 494.0, airline="ZIPAIR Tokyo")

    body = client.get(f"/api/watches/{token}").json()

    # curve.py の係数表からではなく、このルートの実測から候補を出す。
    assert body["available_airlines"] == ["ZIPAIR Tokyo"]


def test_a_selected_airline_stays_selectable_even_if_unobserved(client):
    token = _create(client, destination="LAX",
                    airline_filter=["Some Airline"]).json()["token"]

    body = client.get(f"/api/watches/{token}").json()

    # 候補から消えると「選んだのに外せない」状態になる。
    assert "Some Airline" in body["available_airlines"]


def test_filtering_to_an_airline_that_never_flies_the_leg_shows_nothing(client):
    token = _create(client, destination="SAN").json()["token"]
    _seed_price("NRT", "LAX", DEPARTURE, 494.0, airline="ZIPAIR Tokyo")
    _seed_price("NRT", "SAN", DEPARTURE, 708.0, airline="WestJet")

    client.patch(f"/api/watches/{token}", json={"airline_filter": ["ZIPAIR Tokyo"]})
    body = client.get(f"/api/watches/{token}").json()
    san = next(r for r in body["routes"] if r["destination"] == "SAN")

    # 匿名の曲線を出してはいけない。ZIPAIR は NRT-SAN を飛んでいない。
    assert san["recommendation"] is None
    assert san["series"] == []


def test_updating_an_unknown_token_is_not_found(client):
    assert client.patch("/api/watches/nope-nope-nope",
                        json={"airline_filter": []}).status_code == 404


def test_duplicate_airline_names_are_collapsed(client):
    token = _create(client, destination="LAX").json()["token"]
    client.patch(f"/api/watches/{token}",
                 json={"airline_filter": ["ANA", " ANA ", "", "JAL"]})

    assert client.get(f"/api/watches/{token}").json()["airline_filter"] == ["ANA", "JAL"]


def test_the_airline_filter_narrows_the_series(client):
    token = _create(client, destination="LAX",
                    airline_filter=["ZIPAIR"]).json()["token"]
    _seed_price("NRT", "LAX", DEPARTURE, 2200.0, airline="ANA")
    _seed_price("NRT", "LAX", DEPARTURE, 494.0, airline="ZIPAIR")

    body = client.get(f"/api/watches/{token}").json()

    airlines = {s["airline"] for r in body["routes"] for s in r["series"]}
    assert airlines == {"ZIPAIR"}


def test_simulated_prices_do_not_reach_the_api(client):
    token = _create(client, destination="LAX").json()["token"]
    _seed_price("NRT", "LAX", DEPARTURE, 700.0, airline="ANA")
    _seed_price("NRT", "LAX", DEPARTURE, 1.0, airline="ZIPAIR", source="simulated")

    body = client.get(f"/api/watches/{token}").json()

    airlines = {s["airline"] for r in body["routes"] for s in r["series"]}
    assert "ZIPAIR" not in airlines


def test_opening_a_watch_extends_its_life(client):
    token = _create(client).json()["token"]
    before = repository.get_watch_by_token(token).last_accessed_at
    client.get(f"/api/watches/{token}")
    after = repository.get_watch_by_token(token).last_accessed_at
    assert after >= before


# ======================================================================
# 削除
# ======================================================================

def test_deleting_a_watch_makes_it_unreachable(client):
    token = _create(client).json()["token"]
    assert client.delete(f"/api/watches/{token}").status_code == 204
    assert client.get(f"/api/watches/{token}").status_code == 404


def test_deleting_an_unknown_token_is_not_found(client):
    assert client.delete("/api/watches/nope-nope-nope").status_code == 404


def test_deleting_a_watch_stops_collection_but_keeps_the_history(client):
    token = _create(client, destination="LAX").json()["token"]
    _seed_price("NRT", "LAX", DEPARTURE, 700.0)

    client.delete(f"/api/watches/{token}")

    assert repository.count_active_routes() == 0          # 収集は止まる
    assert repository.history_for("NRT", "LAX", DEPARTURE)  # 履歴は残る


def test_a_route_shared_with_another_watch_keeps_collecting(client):
    keeper = _create(client, destination="LAX", nearby_airports=True).json()["token"]
    doomed = _create(client, destination="LAX", nearby_airports=True).json()["token"]

    client.delete(f"/api/watches/{doomed}")

    # 他のユーザーがまだ同じルートを見ているので止めてはいけない。
    assert repository.count_active_routes() == 2      # NRT-LAX と HND-LAX
    assert client.get(f"/api/watches/{keeper}").status_code == 200


# ======================================================================
# トークンの扱い（2.4 / 3節）
# ======================================================================

def test_the_watch_page_is_noindex(client):
    token = _create(client).json()["token"]
    response = client.get(f"/w/{token}")

    assert response.status_code == 200
    assert "noindex" in response.headers["x-robots-tag"]
    assert "noindex" in response.text          # meta タグ側でも
    assert response.headers["referrer-policy"] == "no-referrer"


def test_the_watch_page_does_not_reveal_whether_a_token_exists(client):
    real = _create(client).json()["token"]
    # 実在しないトークンでも同じ HTML を返す。ここで 404 を出し分けると、
    # HTML が返るかどうかだけで総当たりできてしまう。
    assert client.get(f"/w/{real}").status_code == 200
    assert client.get("/w/not-a-real-token").status_code == 200


def test_chart_js_is_served_locally_not_from_a_cdn(client):
    # CDN から読むと、その Referer ヘッダに /w/{token} がそのまま載る。
    page = client.get("/w/anything").text
    assert "cdn." not in page
    assert client.get("/static/vendor/chart.umd.min.js").status_code == 200


def test_tokens_are_redacted_from_logs():
    from app.security import redact

    # テストデータには本物らしい値を置かない。トークンは capability なので、
    # 実在しないと分かる文字列にしておく。
    fake = "EXAMPLE-TOKEN-not-a-real-one"
    assert redact(f'"GET /w/{fake} HTTP/1.1" 200') == \
        '"GET /w/<token> HTTP/1.1" 200'
    assert redact(f"error at /api/watches/{fake}") == \
        "error at /api/watches/<token>"


def test_a_spoofed_forwarded_header_cannot_dodge_the_rate_limit(client, monkeypatch):
    monkeypatch.setenv("MAX_WATCHES_PER_IP_PER_DAY", "1")
    get_settings.cache_clear()

    assert _create(client, destination="LAX").status_code == 201
    # ヘッダはクライアントが自由に付けられる。既定で信用しないので、
    # 別人のふりをしても上限は素通りできない。
    response = client.post(
        "/api/watches",
        json={"origin": "NRT", "destination": "SFO", "departure_date": DEPARTURE},
        headers={"X-Forwarded-For": "203.0.113.99"},
    )
    assert response.status_code == 429

    get_settings.cache_clear()


def test_the_forwarded_header_is_used_only_when_explicitly_trusted(client, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("MAX_WATCHES_PER_IP_PER_DAY", "1")
    get_settings.cache_clear()

    # リバースプロキシが上書きする構成でだけ true にする想定。
    assert client.post(
        "/api/watches",
        json={"origin": "NRT", "destination": "LAX", "departure_date": DEPARTURE},
        headers={"X-Forwarded-For": "203.0.113.10"},
    ).status_code == 201
    assert client.post(
        "/api/watches",
        json={"origin": "NRT", "destination": "SFO", "departure_date": DEPARTURE},
        headers={"X-Forwarded-For": "203.0.113.11"},
    ).status_code == 201

    get_settings.cache_clear()


def test_the_api_docs_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("DOCS_ENABLED", "false")
    get_settings.cache_clear()

    import importlib

    import app.main
    reloaded = importlib.reload(app.main)
    with TestClient(reloaded.app) as c:
        assert c.get("/api/docs").status_code == 404
        assert c.get("/api/openapi.json").status_code == 404

    get_settings.cache_clear()
    importlib.reload(app.main)


def test_the_log_file_rotates(tmp_path, monkeypatch):
    import logging

    import app.logging_setup as logging_setup

    monkeypatch.setenv("LOG_MAX_BYTES", "1000")
    monkeypatch.setenv("LOG_BACKUP_COUNT", "1")
    get_settings.cache_clear()

    logging_setup._configured = False
    logging.getLogger("app").handlers.clear()
    logging_setup.configure_logging()

    logger = logging.getLogger("app.test")
    for _ in range(80):
        logger.info("x" * 100)

    log_dir = Path(get_settings().db_path).parent
    # 回らないと、常駐プロセスのログが何年も積み上がってディスクを食う。
    assert (log_dir / "app.log.1").exists()
    assert (log_dir / "app.log").stat().st_size < 5000

    logging_setup._configured = False
    logging.getLogger("app").handlers.clear()
    get_settings.cache_clear()


def test_the_stored_ip_is_hashed_not_the_raw_address(client):
    _create(client)
    from app.db.connection import get_connection
    with get_connection() as conn:
        stored = conn.execute(
            "SELECT created_from_ip_hash FROM watches"
        ).fetchone()["created_from_ip_hash"]

    assert stored and "testclient" not in stored
    assert len(stored) == 64          # sha256 の16進表現


def test_the_ip_hash_is_salted(tmp_path, monkeypatch):
    import hashlib

    from app.security import ip_hash

    hashed = ip_hash("203.0.113.7")
    # ソルト無しの sha256 だと IPv4 は総当たりで元に戻せる。
    assert hashed != hashlib.sha256(b"203.0.113.7").hexdigest()


# ======================================================================
# 上限（Q4）
# ======================================================================

def test_the_global_route_cap_rejects_new_watches(client, monkeypatch):
    monkeypatch.setenv("MAX_ACTIVE_ROUTES", "1")
    get_settings.cache_clear()

    assert _create(client, origin="KIX", destination="SFO").status_code == 201
    # 上限到達時は黙って収集を落とさず、明示的に断る。
    response = _create(client, origin="KIX", destination="SEA")
    assert response.status_code == 503
    assert response.json()["detail"]

    get_settings.cache_clear()


def test_the_per_ip_daily_cap_rejects_new_watches(client, monkeypatch):
    monkeypatch.setenv("MAX_WATCHES_PER_IP_PER_DAY", "2")
    get_settings.cache_clear()

    assert _create(client, destination="LAX").status_code == 201
    assert _create(client, destination="SFO").status_code == 201
    assert _create(client, destination="SEA").status_code == 429

    get_settings.cache_clear()


def test_the_per_ip_concurrent_cap_rejects_new_watches(client, monkeypatch):
    monkeypatch.setenv("MAX_ACTIVE_WATCHES_PER_IP", "1")
    get_settings.cache_clear()

    assert _create(client, destination="LAX").status_code == 201
    assert _create(client, destination="SFO").status_code == 429

    get_settings.cache_clear()


def test_the_nearby_rule_counts_every_route_against_the_cap(client, monkeypatch):
    monkeypatch.setenv("MAX_ACTIVE_ROUTES", "3")
    get_settings.cache_clear()

    # NRT->SAN は 4 ルート必要なので、上限3では作れない。
    assert _create(client, destination="SAN", nearby_airports=True).status_code == 503
    # 近接空港ルールを切れば1ルートで収まる。
    assert _create(client, destination="SAN",
                   nearby_airports=False).status_code == 201

    get_settings.cache_clear()


# ======================================================================
# 静的ページ
# ======================================================================

def test_the_index_page_is_served(client):
    assert client.get("/").status_code == 200


def test_the_api_tells_the_screen_when_prices_start_rising(client):
    """画面が読むのはこの値。**無いと「いつまでに買うか」が言えなくなる。**

    実測のある区間で確かめる。実測が1件も無い区間では判定ごと出さない
    ようになったため（2026-08-30）。
    """
    _seed_price("NRT", "LAX", DEPARTURE, 700.0, airline="ANA")
    token = _create(client, destination="LAX").json()["token"]
    body = client.get(f"/api/watches/{token}").json()

    route = next(r for r in body["routes"]
                 if (r["origin"], r["destination"]) == ("NRT", "LAX"))
    rise = route["recommendation"]["price_rises_after"]
    assert rise["date"]
    assert rise["penalty_pct"] > 0


# --- 便が無い区間には推定を出さない（2026-08-30）--------------------
#
# **利用者の報告。** NRT-GMP は運航されていないのに、¥113,300 という
# 「予測の底値」が出ていた。存在しない便の値段を断定的に見せていた。

def test_a_route_with_no_flights_shows_no_estimate(client):
    from app.db import repository

    res = _create(client, origin="NRT", destination="GMP")
    token = res.json()["token"]

    ids = [r.id for r in repository.active_routes()
           if (r.origin, r.destination) == ("NRT", "GMP")]
    repository.disable_routes(ids, "3回連続で便が0件", "no_flights")

    route = next(r for r in client.get(f"/api/watches/{token}").json()["routes"]
                 if (r["origin"], r["destination"]) == ("NRT", "GMP"))

    assert route["stopped_kind"] == "no_flights"
    # **ここが本題。** 値段を1つも出さない。
    assert route["summary"]["best"] is None
    assert route["summary"]["basis"] == "no_flights"
    assert route["recommendation"] is None
    assert route["series"] == []


# --- 運用ダッシュボードの取得口（2026-09-01）------------------------
#
# 毎朝クラウド側のルーティンがここを叩いてダッシュボードを作り直す。
# ルーティンは本番 DB にも S3 にも触れないので、渡す口がここだけ要る。

def test_the_ops_endpoint_is_absent_until_a_key_is_set(client):
    """**鍵が未設定なら存在しないのと同じ。** 「開いているつもりが
    無かった」を起こさないため、404 を返す。"""
    assert client.get("/api/ops/dashboard.json").status_code == 404
    assert client.get("/api/ops/dashboard.json?key=anything").status_code == 404


def test_the_ops_endpoint_rejects_a_wrong_key(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("OPS_API_KEY", "correct-horse")
    get_settings.cache_clear()
    try:
        assert client.get("/api/ops/dashboard.json?key=wrong").status_code == 404
        res = client.get("/api/ops/dashboard.json?key=correct-horse")
        assert res.status_code == 200
        body = res.json()
        # buy_timing は 2026-10-12 から（買い時の答え合わせ）。
        assert set(body) == {"generated_at", "kpi", "drops", "meta", "buy_timing"}
        assert isinstance(body["drops"], list)
        assert "total" in body["meta"]
    finally:
        get_settings.cache_clear()


def test_the_ops_endpoint_never_returns_tokens_or_ip_hashes(client, monkeypatch):
    """**集計だけを返す。** モニターのトークンが漏れると、その監視を
    誰でも書き換えられる（トークン＝権限）。"""
    from app.config import get_settings
    _create(client)
    monkeypatch.setenv("OPS_API_KEY", "k")
    get_settings.cache_clear()
    try:
        raw = client.get("/api/ops/dashboard.json?key=k").text
    finally:
        get_settings.cache_clear()
    assert "token" not in raw.lower()
    assert "ip_hash" not in raw


def test_the_ops_endpoint_serves_the_built_dashboard(client, monkeypatch):
    """**クラウド側に生成器を持たせない。** リポジトリが非公開なので、
    組み立て済みの HTML をここから渡す。"""
    import csv as _csv
    from app.config import get_settings
    from app.kpi import COLUMNS
    _seed_price("NRT", "LAX", DEPARTURE, 700.0, airline="ANA")

    monkeypatch.setenv("OPS_API_KEY", "k")
    get_settings.cache_clear()
    csv_path = get_settings().kpi_csv_path
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for day in ("2026-08-30", "2026-08-31"):
            w.writerow({**{c: "" for c in COLUMNS}, "date": day,
                        "watches_total": 10, "watches_created_last_24h": 1,
                        "distinct_creators": 3, "routes_active": 20,
                        "snapshots_real": 100, "fetch_errors": 0,
                        "series_completed": 0})
    try:
        assert client.get("/api/ops/dashboard.html").status_code == 404
        assert client.get("/api/ops/dashboard.html?key=nope").status_code == 404
        assert client.get("/api/ops/dashboard.html?key=k&lang=fr").status_code == 400

        for lang, marker in (("ja", "運用ダッシュボード"),
                             ("en", "Operations Dashboard")):
            res = client.get(f"/api/ops/dashboard.html?key=k&lang={lang}")
            assert res.status_code == 200, lang
            assert marker in res.text
            # データが埋まっていること（空の殻を配らない）
            assert '<script id="data"' in res.text
            assert '<script id="strings"' in res.text
    finally:
        get_settings.cache_clear()
