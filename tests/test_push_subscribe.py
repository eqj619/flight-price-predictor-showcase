"""通知の購読（課題4・段階A）。

**この段階では何も送らない。** 購読を貯めるだけなので、テストの主題は
「貯め方が正しいか」と「鍵が無いときに通知できるふりをしないか」。
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import repository as repo
from app.main import app

client = TestClient(app)
DEP = (date.today() + timedelta(days=100)).isoformat()
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abcdefghijklmnop1234"


@pytest.fixture
def keys(monkeypatch):
    """VAPID 鍵が設定された状態にする。"""
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BF1HTyO1XYk7JftWAItHiVVT8cGR_test")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "cHJpdmF0ZS1rZXktZm9yLXRlc3Q")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _watch():
    return client.post("/api/watches", json={
        "origin": "NRT", "destination": "LAX", "departure_date": DEP,
    }).json()["token"]


def _subscribe(token, endpoint=ENDPOINT, **kw):
    body = {"endpoint": endpoint, "p256dh": "BPublicKeyForTest0000",
            "auth": "authsecret123", "watch_token": token}
    body.update(kw)
    return client.post("/api/push/subscribe", json=body)


# ============================================================
# 鍵が無いときに、通知できるふりをしない
# ============================================================

def test_without_keys_the_feature_reports_itself_as_off():
    """**画面はこれを見てボタンを出すか決める。**

    鍵を入れ忘れた状態で「通知を受け取る」と見せて、実際には何も来ない、
    という事故を防ぐのがこの返り値の役目。
    """
    get_settings.cache_clear()
    body = client.get("/api/push/key").json()
    assert body["enabled"] is False
    assert body["public_key"] is None


def test_without_keys_subscribing_is_refused():
    get_settings.cache_clear()
    assert _subscribe(_watch()).status_code == 503


def test_with_keys_the_public_key_is_served(keys):
    body = client.get("/api/push/key").json()
    assert body["enabled"] is True
    assert body["public_key"].startswith("B")


# ============================================================
# 購読
# ============================================================

def test_a_subscription_is_stored_and_linked(keys):
    token = _watch()
    res = _subscribe(token)
    assert res.status_code == 201
    assert res.json()["watches"] == [token]
    assert repo.count_push_subscriptions() == 1


def test_subscribing_twice_does_not_duplicate(keys):
    token = _watch()
    _subscribe(token)
    _subscribe(token)
    assert repo.count_push_subscriptions() == 1
    assert repo.watch_tokens_for_subscription(ENDPOINT) == [token]


def test_resubscribing_updates_the_keys(keys):
    """**同じ端末が購読を作り直すと鍵だけ変わることがある。**

    古い鍵のまま送ると復号できず、利用者には「許可したのに来ない」と
    しか見えない。上書きする。
    """
    token = _watch()
    _subscribe(token)
    _subscribe(token, p256dh="BNewPublicKey00000000", auth="newauth98765")

    subs = repo.push_subscriptions_for_watch(repo.get_watch_by_token(token).id)
    assert len(subs) == 1
    assert subs[0].p256dh == "BNewPublicKey00000000"
    assert subs[0].auth == "newauth98765"


def test_one_device_can_follow_several_monitors(keys):
    a, b = _watch(), _watch()
    _subscribe(a)
    _subscribe(b)
    assert repo.count_push_subscriptions() == 1
    assert set(repo.watch_tokens_for_subscription(ENDPOINT)) == {a, b}


def test_subscribing_to_a_missing_monitor_is_404(keys):
    assert _subscribe("nosuchtoken12345").status_code == 404


# ============================================================
# 解除
# ============================================================

def test_unsubscribing_one_monitor_keeps_the_others(keys):
    a, b = _watch(), _watch()
    _subscribe(a)
    _subscribe(b)
    client.post("/api/push/unsubscribe",
                json={"endpoint": ENDPOINT, "watch_token": a})
    assert repo.watch_tokens_for_subscription(ENDPOINT) == [b]
    assert repo.count_push_subscriptions() == 1


def test_unsubscribing_without_a_token_removes_the_device(keys):
    _subscribe(_watch())
    client.post("/api/push/unsubscribe", json={"endpoint": ENDPOINT})
    assert repo.count_push_subscriptions() == 0


def test_unsubscribing_an_unknown_endpoint_is_still_204(keys):
    """**登録済みかどうかを外から探れないようにする。**"""
    res = client.post("/api/push/unsubscribe",
                      json={"endpoint": "https://example.com/unknown-endpoint-xx"})
    assert res.status_code == 204


def test_deleting_a_watch_drops_its_subscriptions(keys):
    token = _watch()
    _subscribe(token)
    client.delete(f"/api/watches/{token}")
    assert repo.watch_tokens_for_subscription(ENDPOINT) == []


# ============================================================
# 入力の検証
# ============================================================

@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/abcdefghij",   # https でない
    "ftp://example.com/aaaaaaaaaaaaaaaaaaaa",
    "short",
])
def test_a_non_https_endpoint_is_rejected(keys, endpoint):
    """**任意の URL を受けると、我々のサーバから任意の宛先へ POST できる。**"""
    assert _subscribe(_watch(), endpoint=endpoint).status_code == 422


def test_an_over_long_endpoint_is_rejected(keys):
    assert _subscribe(_watch(), endpoint="https://x.example/" + "a" * 1200
                      ).status_code == 422


# ── 宛先を既知のプッシュサービスに限る（SEC-003）──────────────
#
# **2026-09-14 まで、検証は `startswith("https://")` だけだった。**
# この受け口は認証なしなので、任意の https URL を登録して我々のサーバから
# 好きな宛先へ POST させられた（SSRF）。出発日を「今日 + 52日」にすれば
# 値上がり通知の条件が翌日に成立するので、発火も攻撃者が仕込めた。

import pytest

from app.api.schemas import PushSubscribe


def _sub(endpoint):
    return PushSubscribe(endpoint=endpoint, p256dh="p" * 20, auth="a" * 12,
                         watch_token="t" * 12)


@pytest.mark.parametrize("endpoint", [
    "https://fcm.googleapis.com/fcm/send/abc123",           # Chrome
    "https://updates.push.services.mozilla.com/wpush/v2/x",  # Firefox
    "https://web.push.apple.com/QAB1c2",                     # Safari（サブドメイン）
    "https://par02p.notify.windows.com/w/?token=x",          # Windows（サブドメイン）
])
def test_known_push_services_are_accepted(endpoint):
    """**実在の購読を壊さないこと。** 本番の3件は fcm と web.push.apple.com。"""
    assert _sub(endpoint).endpoint == endpoint


@pytest.mark.parametrize("endpoint", [
    "https://evil.example/collect",
    "https://169.254.169.254/latest/meta-data/",
    "https://127.0.0.1:8000/api/watches",
    "https://localhost/",
    # **紛らわしい形。** 自前で文字列を切ると取り違える典型。
    "https://fcm.googleapis.com@evil.example/x",      # ホストは evil.example
    "https://fcm.googleapis.com.evil.example/x",      # 接尾辞が違う
    "https://notfcm.googleapis.com/x",                # 前方が違う
])
def test_anything_else_is_refused(endpoint):
    with pytest.raises(Exception):
        _sub(endpoint)


def test_http_is_still_refused():
    with pytest.raises(Exception):
        _sub("http://fcm.googleapis.com/fcm/send/abc123")


def test_the_subdomain_rule_does_not_match_a_bare_suffix():
    """`push.apple.com` の**サブドメイン**は許すが、接尾辞一致では許さない。"""
    from app.api.schemas import _is_known_push_host
    assert _is_known_push_host("web.push.apple.com")
    assert _is_known_push_host("push.apple.com")
    assert not _is_known_push_host("evilpush.apple.com")
    assert not _is_known_push_host("push.apple.com.evil.example")
