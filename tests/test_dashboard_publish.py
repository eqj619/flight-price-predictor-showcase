"""ダッシュボードの S3 書き出しのテスト。

**なぜ S3 経由なのか。** 毎朝更新するのはクラウド側のルーティンだが、
その実行環境は外向き通信が制限されていて本番のドメインには届かない
（2026-09-01 実測）。S3 には届くので、そこに置いて取りに来てもらう。

置くのは集計と HTML だけ。**トークンと IP ハッシュを混ぜない** —
接頭辞を知っていれば誰でも読めるため。
"""
import json

import pytest

from app import dashboard_publish
from app.config import get_settings
from app.db import repository


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl):
        self.objects[Key] = {"body": Body.decode("utf-8"),
                             "type": ContentType, "cache": CacheControl,
                             "bucket": Bucket}


@pytest.fixture
def fake_s3(monkeypatch):
    """boto3 は本番にしか入っていない（kpi.py と同じく関数の中で import
    している）。ここでは偽のモジュールを差し込む。"""
    import sys
    import types
    s3 = FakeS3()
    mod = types.ModuleType("boto3")
    mod.client = lambda *a, **k: s3
    monkeypatch.setitem(sys.modules, "boto3", mod)
    return s3


def _kpi_csv(days=("2026-08-31", "2026-09-01")):
    from app.kpi import COLUMNS
    import csv
    path = get_settings().kpi_csv_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for d in days:
            w.writerow({**{c: "" for c in COLUMNS}, "date": d,
                        "watches_total": 10, "watches_created_last_24h": 1,
                        "distinct_creators": 3, "routes_active": 20,
                        "snapshots_real": 100, "fetch_errors": 0,
                        "series_completed": 0})


def test_nothing_is_written_when_the_destination_is_unset(fake_s3):
    assert "skipped" in dashboard_publish.publish()
    assert fake_s3.objects == {}


def test_it_writes_the_json_and_both_languages(fake_s3, monkeypatch):
    monkeypatch.setenv("DASHBOARD_S3_BUCKET", "b")
    monkeypatch.setenv("DASHBOARD_S3_PREFIX", "pfx")
    get_settings.cache_clear()
    try:
        _kpi_csv()
        out = dashboard_publish.publish()
        assert set(fake_s3.objects) == {
            "d/pfx/dashboard.json",
            "d/pfx/dashboard.ja.html",
            "d/pfx/dashboard.en.html"}
        assert out["kpi_days"] == 2
        # **古いものを配らない。** 毎日入れ替わるので keep させない。
        assert all(o["cache"] == "no-cache" for o in fake_s3.objects.values())
        assert "運用ダッシュボード" in fake_s3.objects["d/pfx/dashboard.ja.html"]["body"]
        assert "Operations Dashboard" in fake_s3.objects["d/pfx/dashboard.en.html"]["body"]
    finally:
        get_settings.cache_clear()


def test_it_never_writes_tokens_or_ip_hashes(fake_s3, monkeypatch):
    """接頭辞を知っていれば誰でも読める場所なので、ここが要。"""
    monkeypatch.setenv("DASHBOARD_S3_BUCKET", "b")
    monkeypatch.setenv("DASHBOARD_S3_PREFIX", "pfx")
    get_settings.cache_clear()
    try:
        rid = repository.upsert_monitored_route("NRT", "SAN", "2026-12-01")
        w = repository.create_watch("NRT", "SAN", "2026-12-01", [rid],
                                    ip_hash="deadbeef")
        _kpi_csv()
        dashboard_publish.publish()
        for obj in fake_s3.objects.values():
            assert w.token not in obj["body"]
            assert "deadbeef" not in obj["body"]
            assert "ip_hash" not in obj["body"]
    finally:
        get_settings.cache_clear()


def test_an_empty_kpi_does_not_overwrite_yesterday(fake_s3, monkeypatch):
    """**空の殻を置かない。** 上書きすると前日まで見えていたものまで壊す。"""
    monkeypatch.setenv("DASHBOARD_S3_BUCKET", "b")
    monkeypatch.setenv("DASHBOARD_S3_PREFIX", "pfx")
    get_settings.cache_clear()
    try:
        path = get_settings().kpi_csv_path
        if path.exists():
            path.unlink()
        assert "skipped" in dashboard_publish.publish()
        assert fake_s3.objects == {}
    finally:
        get_settings.cache_clear()


# --- グラフの解説（2026-09-02）------------------------------------
#
# 利用者の依頼で、各グラフに「読み方」を付けた。**文中の数字は当日の
# データから作る。** 固定文にすると、数字が動いた日に文章のほうが嘘になる。

def test_the_charts_carry_their_own_explanation(fake_s3, monkeypatch):
    monkeypatch.setenv("DASHBOARD_S3_BUCKET", "b")
    monkeypatch.setenv("DASHBOARD_S3_PREFIX", "pfx")
    get_settings.cache_clear()
    try:
        _kpi_csv()
        dashboard_publish.publish()
        for key in ("d/pfx/dashboard.ja.html", "d/pfx/dashboard.en.html"):
            body = fake_s3.objects[key]["body"]
            # 差し込み口と、3枚ぶんの文言が載っていること
            assert 'class="explain"' in body, key
            for k in ("c3_x", "a1_x", "a2_x"):
                assert f'"{k}":' in body, (key, k)
    finally:
        get_settings.cache_clear()


def test_no_dated_claim_is_baked_into_the_page(fake_s3, monkeypatch):
    """**毎日作り直すページに、その日限りの物語を焼き込まない。**

    「新規登録は8月14日を境に止まった」と書いていたが、これは日が経てば
    ただの古い記述になる。方法の説明だけを残し、数字は当日のものから
    組み立てる。
    """
    monkeypatch.setenv("DASHBOARD_S3_BUCKET", "b")
    monkeypatch.setenv("DASHBOARD_S3_PREFIX", "pfx")
    get_settings.cache_clear()
    try:
        _kpi_csv()
        dashboard_publish.publish()
        for key in ("d/pfx/dashboard.ja.html", "d/pfx/dashboard.en.html"):
            body = fake_s3.objects[key]["body"]
            for stale in ("8月14日", "14 August", "2週間かけて落ちた",
                          "two weeks that followed"):
                assert stale not in body, (key, stale)
    finally:
        get_settings.cache_clear()


# ── 本番の経路も文言を埋める ──────────────────────────────────
#
# **2026-09-10 の取りこぼし。** 数字を文言から外して差し込み口にしたとき、
# 埋める処理を CLI 側（build_dashboard.main）だけに書いた。本番の書き出しは
# app/dashboard_build.render_dashboard を通るので埋まらず、S3 に配った
# ダッシュボードに「日次 KPI {days}日ぶん」と波括弧のまま出た。
#
# **試験は main() を呼んでいたので通っていた。** 経路が2つあるものは、
# 本番が通るほうを試験すること。

_SLOTS = ("{days}", "{span}", "{recs}", "{series}", "{dropped}",
          "{research}", "{acc_days}")


def _ops_for_render():
    row = {
        "date": "2026-09-10", "watches_total": 122,
        "watches_created_last_24h": 0, "distinct_creators": 49,
        "watches_accessed_last_7d": 25, "routes_active": 417,
        "snapshots_real": 66684, "fetch_errors": 0, "series_completed": 45,
        "push_subscriptions": 3,
        "prediction_abs_error_pct_near": 8.4, "prediction_bias_pct_near": 1.0,
        "prediction_band_hit_pct_near": 56.5,
        "prediction_abs_error_pct": 4.1, "prediction_band_hit_pct": 66.9,
        "prediction_abs_error_p90_pct_near": 55.5,
        "prediction_bias_p90_pct_near": 52.5,
    }
    return {"kpi": [row, dict(row, date="2026-09-11")],
            "drops": [{"leg": "NRT-LAX", "dep": "2026-11-10", "rt": False,
                       "n": 20, "start": 1200, "low": 816, "cur": 900,
                       "drop_pct": -32.0, "cur_pct": -25.0,
                       "low_dbd": 60, "start_dbd": 90, "done": False}],
            "meta": {"total": 122, "creators": 49, "routes": 417,
                     "research_routes": 225, "recs": 66684, "subs": 3,
                     "done": 45, "series": 463, "dropped": 427}}


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_the_published_html_has_no_unfilled_slots(lang):
    """**波括弧が画面に出ていないこと。** これが本番で起きた。"""
    from app.dashboard_build import render_dashboard
    html = render_dashboard(_ops_for_render(), lang)
    for slot in _SLOTS:
        assert slot not in html, slot


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_the_published_prose_counts_match_the_data(lang):
    """本文の数字が、同じ payload の中身と一致すること。"""
    from app.dashboard_build import render_dashboard
    ops = _ops_for_render()
    html = render_dashboard(ops, lang)
    assert "66,684" in html                       # 実測件数
    assert str(ops["meta"]["series"]) in html     # 系列数
    assert ("2日ぶん" in html) or ("from 2 days" in html)


def test_an_old_feed_without_the_new_meta_still_renders(lang="ja"):
    """取得口が古くて research_routes が無い日でも落ちない。

    **1つの欠けで全部を落とさない。** 分からないものは「—」と出す。
    """
    from app.dashboard_build import render_dashboard
    ops = _ops_for_render()
    del ops["meta"]["research_routes"]
    html = render_dashboard(ops, lang)
    assert "{research}" not in html
    assert "—% は調査用" in html      # 不明は「—%」
