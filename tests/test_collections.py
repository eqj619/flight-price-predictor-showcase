"""一覧（collections）のテスト。

localStorage は「サイトデータを削除」で消える。Cookie も IndexedDB も同じ
操作で消えるので、複数のモニターを持つ利用者が索引ごと失う経路があった。
一覧のトークン1本をブックマークすれば全部戻る、というのがこの仕組み。

**したがって一番大事な性質は「一覧が中身より先に消えないこと」。** 先に
消えると、作った理由そのものが起きる。
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import repository
from app.main import app
from app.routing import expand_routes


DEPARTURE = "2026-12-01"


@pytest.fixture
def client():
    return TestClient(app)


def _watch(origin="NRT", destination="SAN", departure=DEPARTURE):
    route_ids = [repository.upsert_monitored_route(o, d, departure)
                 for o, d in expand_routes(origin, destination)]
    return repository.create_watch(origin, destination, departure, route_ids)


# ----------------------------------------------------------------------
# 中身との関係
# ----------------------------------------------------------------------

def test_collection_outlives_its_watches():
    """**一覧の期限は所属モニターより後。** 先に切れたら索引を失う。"""
    c = repository.create_collection()
    w = _watch()
    repository.add_watch_to_collection(c.token, w.token)

    stored = repository.get_collection_by_token(c.token)
    assert stored.expires_at > w.expires_at


def test_expiry_extends_when_a_longer_watch_is_added():
    c = repository.create_collection()
    near = _watch(departure=(date.today() + timedelta(days=30)).isoformat())
    repository.add_watch_to_collection(c.token, near.token)
    first = repository.get_collection_by_token(c.token).expires_at

    far = _watch(destination="SFO",
                 departure=(date.today() + timedelta(days=300)).isoformat())
    repository.add_watch_to_collection(c.token, far.token)
    assert repository.get_collection_by_token(c.token).expires_at > first


def test_deleting_a_watch_drops_it_from_the_collection():
    """モニターを消したら一覧からも自動で外れる（ON DELETE CASCADE）。"""
    c = repository.create_collection()
    w = _watch()
    repository.add_watch_to_collection(c.token, w.token)
    assert len(repository.watches_in_collection(c.token)) == 1

    repository.delete_watch(w.token)
    assert repository.watches_in_collection(c.token) == []


def test_removing_from_the_list_keeps_the_monitor():
    """一覧から外すのと、モニターを消すのは別。"""
    c = repository.create_collection()
    w = _watch()
    repository.add_watch_to_collection(c.token, w.token)

    assert repository.remove_watch_from_collection(c.token, w.token)
    assert repository.watches_in_collection(c.token) == []
    assert repository.get_watch_by_token(w.token) is not None


def test_adding_twice_does_not_duplicate():
    c = repository.create_collection()
    w = _watch()
    repository.add_watch_to_collection(c.token, w.token)
    repository.add_watch_to_collection(c.token, w.token)
    assert len(repository.watches_in_collection(c.token)) == 1


def test_expired_collection_is_not_readable():
    c = repository.create_collection()
    with repository.get_connection() as conn:
        conn.execute("UPDATE collections SET expires_at = ? WHERE token = ?",
                     ("2020-01-01", c.token))
    assert repository.get_collection_by_token(c.token) is None


def test_lifecycle_removes_expired_collections():
    from app.jobs.daily import run_lifecycle

    c = repository.create_collection()
    with repository.get_connection() as conn:
        conn.execute("UPDATE collections SET expires_at = ? WHERE token = ?",
                     ("2020-01-01", c.token))
    assert run_lifecycle(date.today())["expired_collections"] == 1


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------

def test_create_and_read_through_the_api(client):
    created = client.post("/api/collections")
    assert created.status_code == 201
    token = created.json()["token"]
    assert created.json()["url"] == f"/l/{token}"

    w = _watch()
    assert client.put(
        f"/api/collections/{token}/watches/{w.token}").status_code == 204

    body = client.get(f"/api/collections/{token}").json()
    assert [x["token"] for x in body["watches"]] == [w.token]
    assert body["watches"][0]["origin"] == "NRT"


def test_token_is_not_guessable(client):
    tokens = {client.post("/api/collections").json()["token"] for _ in range(5)}
    assert len(tokens) == 5
    assert all(len(t) >= 20 for t in tokens)


def test_unknown_collection_is_a_plain_404(client):
    """存在しないトークンと失効したトークンを区別しない。"""
    assert client.get("/api/collections/does-not-exist").status_code == 404


def test_adding_an_unknown_watch_fails(client):
    token = client.post("/api/collections").json()["token"]
    assert client.put(
        f"/api/collections/{token}/watches/nope").status_code == 404


def test_collection_page_is_noindex_and_uncacheable(client):
    """一覧の URL も検索エンジンとキャッシュから外す。"""
    res = client.get("/l/anything")
    assert res.status_code == 200
    assert "noindex" in res.headers["X-Robots-Tag"]
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Referrer-Policy"] == "no-referrer"


def test_collection_page_does_not_reveal_whether_the_token_exists(client):
    """HTML の有無でトークンの実在を総当たり判定できないこと。"""
    real = client.post("/api/collections").json()["token"]
    assert client.get(f"/l/{real}").text == client.get("/l/nonexistent").text


def test_api_responses_are_noindex(client):
    token = client.post("/api/collections").json()["token"]
    res = client.get(f"/api/collections/{token}")
    assert "noindex" in res.headers["X-Robots-Tag"]


def test_reading_refreshes_the_expiry(client):
    """開いている限り生かす。ブックマークが死ぬのを防ぐ。"""
    c = repository.create_collection()
    with repository.get_connection() as conn:
        conn.execute("UPDATE collections SET expires_at = ? WHERE token = ?",
                     ((date.today() + timedelta(days=1)).isoformat(), c.token))

    client.get(f"/api/collections/{c.token}")
    refreshed = repository.get_collection_by_token(c.token).expires_at
    expected = date.today() + timedelta(
        days=get_settings().collection_idle_expiry_days)
    assert refreshed == expected.isoformat()


# ----------------------------------------------------------------------
# トークンをログに残さない
# ----------------------------------------------------------------------

def test_collection_paths_are_redacted_in_logs():
    """一覧の URL もモニターと同じく capability なので、ログに平文で残さない。

    **1つのパスにトークンが2つ載る形がある**（一覧に追加する API）。
    接頭辞を `/api/watches/` のように前方まで含めて書くと、後ろ側の
    トークンが伏字にならない。
    """
    from app.security import redact

    assert redact("/l/RNdedcv4xyzABC") == "/l/<token>"
    assert redact("/api/collections/RNdedcv4xyzABC") == "/api/collections/<token>"
    assert (redact("/api/collections/RNdedcv4xyzABC/watches/Tg6Szu9-abcDEF")
            == "/api/collections/<token>/watches/<token>")
    # 既存の形も壊さない
    assert redact("/w/Tg6Szu9-abcDEF") == "/w/<token>"
    assert redact("/api/watches/Tg6Szu9-abcDEF") == "/api/watches/<token>"
    # トークンでないパスは触らない
    assert redact("/static/js/watch.js") == "/static/js/watch.js"
