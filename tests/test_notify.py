"""通知の送信（課題4・段階B）。①最安の更新だけ。

**主題は「送りすぎないこと」と「嘘を送らないこと」。**
Web Push の許可は一度断られると出し直せないので、薄い通知や誤った通知の
代償は「その1通が無駄になる」では済まない。
"""
from datetime import date, timedelta

import pytest

from app import notify
from app.config import get_settings
from app.db import repository as repo

TODAY = date(2026, 8, 20)
DEP = "2026-12-01"


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BTestPublic")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "testprivatekey")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class Recorder:
    """送信のかわりに記録するだけ。結果は差し替えられる。"""

    def __init__(self, result="sent"):
        self.sent = []
        self.result = result

    def __call__(self, subscription, payload, settings):
        self.sent.append((subscription.endpoint, payload))
        return self.result


def _watch(return_date="", **kw):
    route_id = repo.upsert_monitored_route("NRT", "LAX", DEP, return_date)
    return repo.create_watch(origin="NRT", destination="LAX",
                             departure_date=DEP, route_ids=[route_id],
                             return_date=return_date, **kw)


def _subscribe(watch, endpoint="https://push.example/aaaaaaaaaaaaaaaa", lang="en"):
    sid = repo.upsert_push_subscription(endpoint, "BKey0000", "auth0000", lang)
    repo.link_push_subscription(sid, watch.token)
    return sid


def _price(day, price, return_date=""):
    repo.upsert_snapshots([repo.Snapshot(
        check_date=day, departure_date=DEP, origin="NRT", destination="LAX",
        stops=0, via_airport=None, airline="ANA", price_usd=price,
        source="real", fetched_at="x", return_date=return_date)])


# ============================================================
# 送る条件
# ============================================================

def test_a_new_low_beyond_one_percent_is_sent(keys):
    w = _watch(); _subscribe(w)
    _price("2026-08-18", 600.0)
    _price("2026-08-19", 610.0)
    _price("2026-08-20", 560.0)          # 前日までの最安 600 を 6.7% 下回る

    rec = Recorder()
    result = notify.run(TODAY, sender=rec)
    assert result["low"] == 1 and result["sent"] == 1
    assert len(rec.sent) == 1


def test_a_shallow_new_low_is_not_sent(keys):
    """**0.5% の更新では送らない。** 更新幅の54%がこの薄さ。"""
    w = _watch(); _subscribe(w)
    _price("2026-08-19", 600.0)
    _price("2026-08-20", 597.0)          # 0.5% 下

    rec = Recorder()
    assert notify.run(TODAY, sender=rec)["low"] == 0
    assert rec.sent == []


def test_a_price_rise_is_not_sent(keys):
    w = _watch(); _subscribe(w)
    _price("2026-08-19", 600.0)
    _price("2026-08-20", 700.0)
    assert notify.run(TODAY, sender=Recorder())["low"] == 0


def test_the_first_day_never_counts_as_a_new_low(keys):
    """**比べる相手がいない日に「最安を更新」とは言えない。**"""
    w = _watch(); _subscribe(w)
    _price("2026-08-20", 400.0)
    assert notify.run(TODAY, sender=Recorder())["low"] == 0


def test_a_departed_monitor_is_skipped(keys):
    route_id = repo.upsert_monitored_route("NRT", "LAX", "2026-08-01")
    w = repo.create_watch(origin="NRT", destination="LAX",
                          departure_date="2026-08-01", route_ids=[route_id])
    _subscribe(w)
    assert notify.run(TODAY, sender=Recorder())["low"] == 0


def test_nothing_is_sent_without_keys():
    """鍵が無ければ動かない。**設定漏れで黙って送らないより明示する。**"""
    get_settings.cache_clear()
    w = _watch(); _subscribe(w)
    _price("2026-08-19", 600.0)
    _price("2026-08-20", 500.0)
    result = notify.run(TODAY, sender=Recorder())
    assert result["sent"] == 0 and "skipped" in result


# ============================================================
# 二度送らない
# ============================================================

def test_the_same_low_is_not_sent_twice(keys):
    """日次ジョブが二度走っても再送しない。"""
    w = _watch(); _subscribe(w)
    _price("2026-08-19", 600.0)
    _price("2026-08-20", 500.0)

    rec = Recorder()
    assert notify.run(TODAY, sender=rec)["sent"] == 1
    assert notify.run(TODAY, sender=rec)["low"] == 0
    assert len(rec.sent) == 1


def test_a_further_drop_sends_again(keys):
    w = _watch(); _subscribe(w)
    _price("2026-08-19", 600.0)
    _price("2026-08-20", 500.0)
    notify.run(TODAY, sender=Recorder())

    _price("2026-08-21", 450.0)
    rec = Recorder()
    assert notify.run(date(2026, 8, 21), sender=rec)["sent"] == 1


# ============================================================
# 推定では送らない
# ============================================================

def test_a_monitor_with_no_observations_sends_nothing(keys):
    """曲線の推定しか無いモニターに「最安を更新」は言えない。"""
    w = _watch(); _subscribe(w)
    result = notify.run(TODAY, sender=Recorder())
    assert result["checked"] == 1 and result["low"] == 0


# ============================================================
# 死んだ購読の掃除
# ============================================================

def test_a_gone_subscription_is_deleted(keys):
    """**404/410 は購読が死んでいる。** こちらから死活監視はできない。"""
    w = _watch(); _subscribe(w)
    _price("2026-08-19", 600.0)
    _price("2026-08-20", 500.0)

    result = notify.run(TODAY, sender=Recorder("gone"))
    assert result["gone"] == 1
    assert repo.count_push_subscriptions() == 0


def test_a_failed_send_keeps_the_subscription(keys):
    """一時的な失敗で購読を捨てない。"""
    w = _watch(); _subscribe(w)
    _price("2026-08-19", 600.0)
    _price("2026-08-20", 500.0)

    assert notify.run(TODAY, sender=Recorder("failed"))["failed"] == 1
    assert repo.count_push_subscriptions() == 1


def test_one_broken_subscription_does_not_stop_the_others(keys):
    w = _watch()
    _subscribe(w, "https://push.example/aaaaaaaaaaaaaaaa")
    _subscribe(w, "https://push.example/bbbbbbbbbbbbbbbb")
    _price("2026-08-19", 600.0)
    _price("2026-08-20", 500.0)

    calls = []
    def flaky(sub, payload, settings):
        calls.append(sub.endpoint)
        return "gone" if sub.endpoint.endswith("aaaa") else "sent"

    result = notify.run(TODAY, sender=flaky)
    assert len(calls) == 2
    assert result["sent"] == 1 and result["gone"] == 1


# ============================================================
# 文面
# ============================================================

def test_the_message_says_what_it_knows_and_no_more(keys):
    w = _watch(); _subscribe(w, lang="ja")
    _price("2026-08-19", 600.0)
    _price("2026-08-20", 500.0)

    rec = Recorder()
    notify.run(TODAY, sender=rec)
    _, payload = rec.sent[0]

    assert "NRT" in payload["title"] and "LAX" in payload["title"]
    assert payload["url"] == f"/w/{w.token}"
    # **「買うべき」とは言わない。** 谷の深さは -4.2% で、待てばもっと
    # 下がるかは我々には言えない。
    for word in ("買い", "今すぐ", "お早め", "べき"):
        assert word not in payload["body"], f"断定的な語が入っている: {word}"


def test_the_language_follows_the_subscription(keys):
    w = _watch(); _subscribe(w, lang="en")
    _price("2026-08-19", 600.0)
    _price("2026-08-20", 500.0)
    rec = Recorder()
    notify.run(TODAY, sender=rec)
    assert "New low" in rec.sent[0][1]["title"]


def test_a_round_trip_message_says_it_is_a_round_trip(keys):
    """**往復合計であることを言わないと、片道と比べて高く見える。**"""
    w = _watch(return_date="2026-12-11")
    _subscribe(w, lang="en")
    _price("2026-08-19", 1600.0, "2026-12-11")
    _price("2026-08-20", 1400.0, "2026-12-11")

    rec = Recorder()
    notify.run(TODAY, sender=rec)
    assert "round trip" in rec.sent[0][1]["body"].lower()


def test_round_trips_still_get_the_new_low_notice(keys):
    """①は実測なので往復にも送れる（②は曲線に依存するので段階C）。"""
    w = _watch(return_date="2026-12-11")
    _subscribe(w)
    _price("2026-08-19", 1600.0, "2026-12-11")
    _price("2026-08-20", 1400.0, "2026-12-11")
    assert notify.run(TODAY, sender=Recorder())["sent"] == 1


def test_one_way_and_round_trip_lows_do_not_cross(keys):
    """片道の値下がりで往復の通知を出さない。"""
    ow = _watch()
    rt = _watch(return_date="2026-12-11")
    _subscribe(ow, "https://push.example/oooooooooooooooo")
    _subscribe(rt, "https://push.example/rrrrrrrrrrrrrrrr")
    _price("2026-08-19", 600.0)
    _price("2026-08-20", 500.0)          # 片道だけ下がる
    _price("2026-08-19", 1600.0, "2026-12-11")
    _price("2026-08-20", 1600.0, "2026-12-11")

    rec = Recorder()
    notify.run(TODAY, sender=rec)
    assert len(rec.sent) == 1
    assert rec.sent[0][0].endswith("oooo")


# ============================================================
# ② 値上がりの警告（段階C）
# ============================================================

def _watch_departing_in(days, created_days_ago=60, return_date=""):
    """出発まで days 日のモニター。created_at をずらせる。"""
    from datetime import datetime
    dep = (TODAY + timedelta(days=days)).isoformat()
    route_id = repo.upsert_monitored_route("NRT", "LAX", dep, return_date)
    w = repo.create_watch(origin="NRT", destination="LAX", departure_date=dep,
                          route_ids=[route_id], return_date=return_date)
    created = (TODAY - timedelta(days=created_days_ago)).isoformat() + "T00:00:00"
    from app.db.connection import get_connection
    with get_connection() as conn:
        conn.execute("UPDATE watches SET created_at = ? WHERE id = ?",
                     (created, w.id))
    return repo.get_watch_by_token(w.token)


def test_the_rise_warning_fires_on_the_date_the_screen_shows(keys):
    """**画面と同じ日に鳴る。** 別の日に来ると説明できない。

    画面は price_rises_after の日付を「値上がり開始日」として出している。
    曲線の sweet_spot_start_days から決まる（日本発→米西海岸は65日、方向で違う）。
    """
    from app.predict.curve import route_params
    lead = route_params("NRT", "LAX").sweet_spot_start_days

    w = _watch_departing_in(lead)          # ちょうど値上がり開始日
    _subscribe(w)
    rec = Recorder()
    result = notify.run(TODAY, sender=rec)
    assert result["rise"] == 1 and result["sent"] == 1
    assert "30" in rec.sent[0][1]["body"] or "17" in rec.sent[0][1]["body"]


def test_the_rise_warning_does_not_fire_early(keys):
    from app.predict.curve import route_params
    w = _watch_departing_in(route_params("NRT", "LAX").sweet_spot_start_days + 5)
    _subscribe(w)
    assert notify.run(TODAY, sender=Recorder())["rise"] == 0


def test_the_rise_warning_fires_only_once(keys):
    from app.predict.curve import route_params
    lead = route_params("NRT", "LAX").sweet_spot_start_days
    w = _watch_departing_in(lead)
    _subscribe(w)

    rec = Recorder()
    assert notify.run(TODAY, sender=rec)["rise"] == 1
    # 翌日以降も条件は満たし続けるが、生涯1回。
    assert notify.run(TODAY + timedelta(days=1), sender=rec)["rise"] == 0
    assert len(rec.sent) == 1


def test_a_monitor_created_after_the_rise_date_never_gets_the_warning(keys):
    """**「これから上がります」であって「もう上がっています」ではない。**"""
    from app.predict.curve import route_params
    w = _watch_departing_in(route_params("NRT", "LAX").sweet_spot_start_days - 10,
                            created_days_ago=1)
    _subscribe(w)
    assert notify.run(TODAY, sender=Recorder())["rise"] == 0


def test_a_missed_day_is_still_caught(keys):
    """日次ジョブが1日飛んでも取りこぼさない。"""
    from app.predict.curve import route_params
    w = _watch_departing_in(route_params("NRT", "LAX").sweet_spot_start_days - 2)
    _subscribe(w)
    assert notify.run(TODAY, sender=Recorder())["rise"] == 1


def test_round_trips_never_get_the_rise_warning(keys):
    """**遅延の罰は往復で未測定。** 片道の値を流用しない。"""
    from app.predict.curve import route_params
    w = _watch_departing_in(route_params("NRT", "LAX").sweet_spot_start_days,
                            return_date=(TODAY + timedelta(days=200)).isoformat())
    _subscribe(w)
    assert notify.run(TODAY, sender=Recorder())["rise"] == 0


def test_the_rise_warning_wins_over_a_new_low_on_the_same_day(keys):
    """**②を優先する。** 生涯1回で時期が決まっている。①は明日もある。"""
    from app.predict.curve import route_params
    lead = route_params("NRT", "LAX").sweet_spot_start_days
    dep = (TODAY + timedelta(days=lead)).isoformat()
    w = _watch_departing_in(lead)
    _subscribe(w)
    for day, price in (("2026-08-19", 600.0), ("2026-08-20", 500.0)):
        repo.upsert_snapshots([repo.Snapshot(
            check_date=day, departure_date=dep, origin="NRT", destination="LAX",
            stops=0, via_airport=None, airline="ANA", price_usd=price,
            source="real", fetched_at="x")])

    rec = Recorder()
    result = notify.run(TODAY, sender=rec)
    assert result["rise"] == 1 and result["low"] == 0
    assert len(rec.sent) == 1                       # 1日1通
    assert "rise-" in rec.sent[0][1]["tag"]


def test_the_rise_message_does_not_tell_people_to_buy(keys):
    """言えるのは「遅らせると高い」まで。「いま買え」ではない。"""
    from app.predict.curve import route_params
    w = _watch_departing_in(route_params("NRT", "LAX").sweet_spot_start_days)
    _subscribe(w, lang="ja")
    rec = Recorder()
    notify.run(TODAY, sender=rec)
    body = rec.sent[0][1]["body"]
    for word in ("買い", "今すぐ", "お早め", "べき", "急"):
        assert word not in body, f"断定的な語が入っている: {word}"


def test_a_long_past_rise_date_is_never_sent(keys):
    """**通知を作る前に開始日を過ぎたモニターが、一斉に鳴らないこと。**

    購読が付いた瞬間に「ここから値上がりします」が届くと、事実として
    誤っている（もう上がっている）。本番には該当しうるモニターが36件
    あった（2026-08-21 に発見）。
    """
    from app.predict.curve import route_params
    lead = route_params("NRT", "LAX").sweet_spot_start_days
    # 開始日を20日過ぎている。作成はそれより前。
    w = _watch_departing_in(lead - 20, created_days_ago=60)
    _subscribe(w)

    rec = Recorder()
    assert notify.run(TODAY, sender=rec)["rise"] == 0
    assert rec.sent == []
    # 記録されるので、翌日以降に評価し直さない。
    assert repo.get_watch_by_token(w.token).notified_rise_at is not None


def test_a_few_missed_days_are_still_delivered(keys):
    """猶予の内側なら送る。日次ジョブが数日飛んだ場合を拾うため。"""
    from app.predict.curve import route_params
    w = _watch_departing_in(route_params("NRT", "LAX").sweet_spot_start_days - 3,
                            created_days_ago=60)
    _subscribe(w)
    assert notify.run(TODAY, sender=Recorder())["rise"] == 1


# ── 送信にタイムアウトを掛ける ──────────────────────────────
#
# **pywebpush は既定で無期限に待つ。** `webpush()` の既定は `timeout=None` で、
# それが `send()` の `kwargs.pop("timeout", 10000)` に「存在するキー」として
# 渡るため、既定の 10000 ではなく None が採用される。
#
# 購読の endpoint は認証なしで登録できるので、応答を返さない宛先を
# 仕掛けられると日次ジョブの通知段で固まる。段の並びは
# `… → notifications → kpi → dashboard` なので、KPI とダッシュボードまで
# 止まる。例外ではないので try/except では捕まらない。

def test_the_send_passes_an_explicit_timeout(keys, monkeypatch):
    """**明示しないと無期限に待つ。** 既定に頼らないことを固定する。"""
    from app import notify

    captured = {}

    def fake_webpush(**kwargs):
        captured.update(kwargs)

    monkeypatch.setitem(
        __import__("sys").modules, "pywebpush",
        type("M", (), {"webpush": staticmethod(fake_webpush),
                       "WebPushException": Exception})())

    sub = repo.PushSubscription(
        id=1, endpoint="https://fcm.googleapis.com/x", p256dh="p" * 20,
        auth="a" * 12, lang="ja")
    assert notify._send(sub, {"title": "t", "body": "b"}, get_settings()) == "sent"

    assert "timeout" in captured, "timeout を渡していない（既定は None = 無期限）"
    assert captured["timeout"] == notify.SEND_TIMEOUT_SECONDS
    assert 0 < captured["timeout"] <= 30, "長すぎる値は掛けていないのと大差ない"


def test_pywebpush_would_wait_forever_without_it():
    """前提の確認。**ライブラリ側の既定が変わったら、ここが鳴る。**

    `webpush()` の既定引数が None であること、`send()` がそれを
    「存在するキー」として受け取ること（＝ pop の既定 10000 が効かないこと）
    を、署名と実装から確かめる。
    """
    import inspect

    import pywebpush

    assert inspect.signature(pywebpush.webpush).parameters["timeout"].default is None
    source = inspect.getsource(pywebpush.webpush)
    assert "timeout=timeout" in source, "webpush が timeout を素通しする前提が崩れた"
