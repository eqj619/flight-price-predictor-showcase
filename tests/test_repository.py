"""リポジトリ層のテスト。

「動く」ことより、設計上守らなければならない不変条件が壊れていないことを
確かめるのを主眼にしている（既存 flight-price-monitor/tests の方針を踏襲）。
"""
from datetime import date, datetime, timedelta

import pytest

from app.config import get_settings
from app.db import connection, repository as repo
from app.routing import expand_routes


# ============================================================
# スキーマ
# ============================================================

def test_init_db_creates_all_tables():
    tables = set(connection.table_names())
    assert {
        "monitored_routes", "watches", "watch_routes", "snapshots",
        "prediction_log", "collection_logs", "fetch_errors",
    } <= tables


def test_init_db_is_idempotent():
    connection.init_db()
    connection.init_db()
    assert "watches" in connection.table_names()


# ============================================================
# 近接空港ルール（2.3）
# ============================================================

def test_expansion_covers_both_ends():
    """出発側・到着側の組み合わせで展開する。

    片側ずつに絞ると SAN->HND から LAX->NRT が漏れる。実測で最安だった
    ZIPAIR はまさに NRT-LAX 路線なので、取りこぼす額が大きい。
    """
    assert set(expand_routes("SAN", "HND", nearby_airports=True)) == {
        ("SAN", "HND"), ("SAN", "NRT"), ("LAX", "HND"), ("LAX", "NRT")}
    # 要求されたルートは必ず先頭に残す。
    assert expand_routes("SAN", "HND", nearby_airports=True)[0] == ("SAN", "HND")


def test_neither_side_expansion_creates_a_lax_lax_route():
    assert expand_routes("SAN", "LAX", nearby_airports=True) == [("SAN", "LAX")]
    assert expand_routes("LAX", "SAN", nearby_airports=True) == [("LAX", "SAN")]


def test_an_existing_watch_can_be_backfilled_with_a_new_route():
    """近接空港ルールを広げたとき、登録済みモニターに後からルートを足せること。

    expand_routes は登録時にしか走らないので、ルールを変えただけでは既存
    モニターに反映されない。CLI の sync-routes がこの経路を使う。
    """
    departure = "2026-10-08"
    # ルール拡張前の状態（SAN->HND が1ルートしか持っていない）を作る。
    old_id = repo.upsert_monitored_route("SAN", "HND", departure)
    watch = repo.create_watch("SAN", "HND", departure, [old_id])
    assert len(repo.routes_for_watch(watch.token)) == 1

    # 拡張後の展開を流し直す。
    route_ids = [
        repo.upsert_monitored_route(o, d, departure)
        for o, d in expand_routes("SAN", "HND", nearby_airports=True)
    ]
    added = repo.link_watch_routes(watch.id, route_ids)

    assert added == 3
    assert {(r.origin, r.destination)
            for r in repo.routes_for_watch(watch.token)} == {
        ("SAN", "HND"), ("SAN", "NRT"), ("LAX", "HND"), ("LAX", "NRT")}


def test_backfilling_twice_adds_nothing_the_second_time():
    departure = "2026-10-08"
    route_ids = [repo.upsert_monitored_route(o, d, departure)
                 for o, d in expand_routes("SAN", "HND", nearby_airports=True)]
    watch = repo.create_watch("SAN", "HND", departure, route_ids)

    assert repo.link_watch_routes(watch.id, route_ids) == 0


def test_san_expands_to_lax_but_not_lax_san():
    routes = expand_routes("HND", "SAN", nearby_airports=True)
    assert ("HND", "SAN") in routes
    assert ("HND", "LAX") in routes
    # LAX-SAN 間の便自体は不要
    assert ("LAX", "SAN") not in routes


def test_non_special_route_expands_to_itself_only():
    assert expand_routes("KIX", "JFK", nearby_airports=True) == [("KIX", "JFK")]


def test_tokyo_airports_are_interchangeable():
    # HND と NRT はどちらも東京。利用者にとって入れ替え可能。
    assert set(expand_routes("HND", "JFK", nearby_airports=True)) == {("HND", "JFK"), ("NRT", "JFK")}


def test_nearby_rule_can_be_switched_off():
    assert expand_routes("SAN", "HND", nearby_airports=False) == [("SAN", "HND")]


def test_expansion_is_capped():
    # 近接ペアを増やすとルート数は掛け算で伸びる。歯止めが要る。
    assert len(expand_routes("SAN", "HND", nearby_airports=True,
                             max_routes=2)) == 2


def test_departure_from_nearby_airport_does_not_self_loop():
    # LAX 発 SAN 行きを LAX->LAX に展開してしまわないこと
    assert ("LAX", "LAX") not in expand_routes("LAX", "SAN", nearby_airports=True)


# ============================================================
# 収集の重複排除（2.4 の中核）
# ============================================================

def test_same_route_from_two_watches_is_collected_once():
    """複数ユーザーが同じルート・同じ出発日を登録しても収集対象は1件。

    API コストがユーザー数ではなく異なるルート数に比例する、という
    設計の要。ここが壊れるとコストが線形に増える。
    """
    rid_a = repo.upsert_monitored_route("KIX", "SFO", "2026-12-20")
    rid_b = repo.upsert_monitored_route("KIX", "SFO", "2026-12-20")
    assert rid_a == rid_b

    repo.create_watch("KIX", "SFO", "2026-12-20", [rid_a])
    repo.create_watch("KIX", "SFO", "2026-12-20", [rid_b])

    assert repo.count_active_routes() == 1
    assert len(repo.active_routes()) == 1


def test_watch_with_san_rule_links_two_routes():
    """watch : monitored_route は多対一ではなく多対多。"""
    ids = [repo.upsert_monitored_route(o, d, "2026-12-20")
           for o, d in expand_routes("HND", "SAN", nearby_airports=True)]
    watch = repo.create_watch("KIX", "SFO", "2026-12-20", ids)

    linked = repo.routes_for_watch(watch.token)
    assert {(r.origin, r.destination) for r in linked} == {
        ("HND", "SAN"), ("HND", "LAX"), ("NRT", "SAN"), ("NRT", "LAX")}


# ============================================================
# トークン（2.4）
# ============================================================

def test_tokens_are_unique_and_not_sequential():
    rid = repo.upsert_monitored_route("KIX", "SFO", "2026-12-20")
    tokens = {repo.create_watch("KIX", "SFO", "2026-12-20", [rid]).token
              for _ in range(50)}
    assert len(tokens) == 50
    # 連番 ID は不可。長さ・文字種が推測困難であることの最低限の確認。
    for t in tokens:
        assert len(t) >= 20
        assert not t.isdigit()


def test_watch_is_retrievable_by_token_only():
    rid = repo.upsert_monitored_route("NRT", "SAN", "2026-11-01")
    watch = repo.create_watch("NRT", "SAN", "2026-11-01", [rid],
                              airline_filter=["ZIPAIR"])

    found = repo.get_watch_by_token(watch.token)
    assert found is not None
    assert found.origin == "NRT"
    assert found.airline_filter == ["ZIPAIR"]
    assert repo.get_watch_by_token("nonexistent-token") is None


# ============================================================
# ライフサイクル（2.4 — 匿名ゆえに必須）
# ============================================================

def test_expiry_is_the_earlier_of_idle_limit_and_departure_grace():
    settings = get_settings()
    # 出発日が近い場合は「出発日 + 猶予」が効く
    near = (date.today() + timedelta(days=3)).isoformat()
    rid = repo.upsert_monitored_route("HND", "JFK", near)
    watch = repo.create_watch("HND", "JFK", near, [rid])

    expected = (date.fromisoformat(near)
                + timedelta(days=settings.watch_post_departure_grace_days))
    assert watch.expires_at == expected.isoformat()


def test_a_far_departure_is_still_collected_to_the_end():
    """既定では放置期限で系列を切らない（collect_until_departure=True）。

    途中で切れた系列は「残り期間の最小値がいくらだったか」が確定せず、
    価格曲線の推定にも学習にも使えない。
    """
    settings = get_settings()
    far = (date.today() + timedelta(days=365)).isoformat()
    rid = repo.upsert_monitored_route("KIX", "JFK", far)
    watch = repo.create_watch("KIX", "JFK", far, [rid])

    expected = (date.fromisoformat(far)
                + timedelta(days=settings.watch_post_departure_grace_days))
    assert watch.expires_at == expected.isoformat()


def test_the_idle_limit_applies_when_the_setting_is_off(monkeypatch):
    monkeypatch.setenv("COLLECT_UNTIL_DEPARTURE", "false")
    get_settings.cache_clear()
    settings = get_settings()

    far = (date.today() + timedelta(days=365)).isoformat()
    rid = repo.upsert_monitored_route("KIX", "JFK", far)
    watch = repo.create_watch("KIX", "JFK", far, [rid])

    # 仕様 2.4 当初の「早い方」。
    expected = date.today() + timedelta(days=settings.watch_idle_expiry_days)
    assert watch.expires_at == expected.isoformat()

    get_settings.cache_clear()


def test_orphan_route_is_deactivated_when_last_watch_is_deleted():
    rid = repo.upsert_monitored_route("KIX", "SFO", "2026-12-20")
    watch = repo.create_watch("KIX", "SFO", "2026-12-20", [rid])

    assert repo.count_active_routes() == 1
    repo.delete_watch(watch.token)
    assert repo.deactivate_orphan_routes() == 1
    assert repo.count_active_routes() == 0


def test_route_stays_active_while_another_watch_still_references_it():
    rid = repo.upsert_monitored_route("KIX", "SFO", "2026-12-20")
    a = repo.create_watch("KIX", "SFO", "2026-12-20", [rid])
    repo.create_watch("KIX", "SFO", "2026-12-20", [rid])

    repo.delete_watch(a.token)
    repo.deactivate_orphan_routes()
    assert repo.count_active_routes() == 1


def test_departed_routes_are_deactivated():
    past = (date.today() - timedelta(days=1)).isoformat()
    future = (date.today() + timedelta(days=30)).isoformat()
    repo.upsert_monitored_route("KIX", "SFO", past)
    repo.upsert_monitored_route("KIX", "SFO", future)

    assert repo.deactivate_departed_routes(date.today().isoformat()) == 1
    assert repo.count_active_routes() == 1


def test_deleting_a_watch_keeps_the_price_history():
    """モニターが消えても収集済みの価格は資産として残す（3節）。"""
    rid = repo.upsert_monitored_route("KIX", "SFO", "2026-12-20")
    watch = repo.create_watch("KIX", "SFO", "2026-12-20", [rid])
    repo.upsert_snapshots([_snap("2026-08-08", price=800.0)])

    repo.delete_watch(watch.token)

    assert len(repo.history_for("HND", "SAN", "2026-12-20")) == 1


def test_touch_watch_extends_expiry():
    far = (date.today() + timedelta(days=365)).isoformat()
    rid = repo.upsert_monitored_route("HND", "JFK", far)
    watch = repo.create_watch("HND", "JFK", far, [rid])

    repo.touch_watch(watch.token)
    refreshed = repo.get_watch_by_token(watch.token)
    assert refreshed.last_accessed_at >= watch.created_at
    assert refreshed.expires_at == watch.expires_at  # 同日なら変わらない


# ============================================================
# snapshots
# ============================================================

def _snap(check_date: str, *, stops: int = 1, via: str | None = "LAX",
          airline: str = "ANA", price: float = 900.0,
          source: str = "real") -> repo.Snapshot:
    return repo.Snapshot(
        check_date=check_date, departure_date="2026-12-20",
        origin="HND", destination="SAN", stops=stops, via_airport=via,
        airline=airline, price_usd=price, source=source,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
    )


def test_upsert_overwrites_the_same_key():
    repo.upsert_snapshots([_snap("2026-08-08", price=900.0)])
    repo.upsert_snapshots([_snap("2026-08-08", price=850.0)])

    rows = repo.history_for("HND", "SAN", "2026-12-20")
    assert len(rows) == 1
    assert rows[0]["price_usd"] == 850.0


def test_nonstop_rows_are_deduplicated_despite_null_via_airport():
    """via_airport を NULL のまま入れると UNIQUE が効かなくなる罠の回帰テスト。

    SQLite の UNIQUE 制約は NULL != NULL と評価するため、nonstop 行を
    NULL で持つと同じキーの行が何本でも入ってしまう。'' に落として
    保存しているので1本に収まる。
    """
    repo.upsert_snapshots([_snap("2026-08-08", stops=0, via=None, price=1200.0)])
    repo.upsert_snapshots([_snap("2026-08-08", stops=0, via=None, price=1150.0)])

    rows = repo.history_for("HND", "SAN", "2026-12-20")
    assert len(rows) == 1
    assert rows[0]["price_usd"] == 1150.0
    # 読み出し時は '' を None に戻す
    assert rows[0]["via_airport"] is None


def test_real_and_simulated_rows_coexist_without_colliding():
    repo.upsert_snapshots([
        _snap("2026-08-08", airline="ANA", price=900.0, source="real"),
        _snap("2026-08-09", airline="ANA", price=880.0, source="simulated"),
    ])
    assert len(repo.history_for("HND", "SAN", "2026-12-20",
                                sources=("real", "simulated"))) == 2


def test_history_excludes_simulated_rows_by_default():
    """既存 predict.py の欠陥（Phase 0 で発見）の回帰テスト。

    source を区別せず全行を返していたため、バックフィルしたシミュレーション
    値が「実測」として較正に混入し、最安値として推奨されうる状態だった。
    既定は実測のみ。
    """
    repo.upsert_snapshots([
        _snap("2026-08-08", airline="ANA", price=900.0, source="real"),
        _snap("2026-08-08", airline="JAL", price=100.0, source="simulated"),
    ])

    default_rows = repo.history_for("HND", "SAN", "2026-12-20")
    assert [r["source"] for r in default_rows] == ["real"]
    # 明示的に広げたときだけシミュレーション値が見える
    assert len(repo.history_for("HND", "SAN", "2026-12-20",
                                sources=("real", "simulated"))) == 2


def test_invalid_source_is_rejected():
    with pytest.raises(Exception):
        repo.upsert_snapshots([_snap("2026-08-08", source="guessed")])


def test_latest_check_date():
    assert repo.latest_check_date() is None
    repo.upsert_snapshots([_snap("2026-08-08"), _snap("2026-08-10")])
    assert repo.latest_check_date() == "2026-08-10"


# ============================================================
# prediction_log / ログ
# ============================================================

def test_predictions_are_upserted_with_their_confidence_band():
    p = repo.Prediction(
        predicted_on="2026-08-08", target_check_date="2026-08-15",
        departure_date="2026-12-20", origin="HND", destination="SAN",
        stops=1, via_airport="LAX", airline="ANA",
        predicted_usd=850.0, lower_usd=780.0, upper_usd=920.0,
        observed_points=3,
    )
    assert repo.insert_predictions([p]) == 1
    # 同じキーで上書きされる（同日に2回予測しても行が増えない）
    repo.insert_predictions([p])

    with connection.get_connection() as conn:
        rows = conn.execute("SELECT * FROM prediction_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["lower_usd"] < rows[0]["predicted_usd"] < rows[0]["upper_usd"]
    assert rows[0]["observed_points"] == 3


def test_collection_log_roundtrip():
    repo.insert_collection_log("2026-08-08", "partial", "scheduler",
                                routes_checked=10, records_saved=42,
                                errors=["HND-SAN timeout"], duration_ms=1234)
    log = repo.latest_collection_log()
    assert log["status"] == "partial"
    assert log["records_saved"] == 42
    assert "timeout" in log["errors"]


def test_invalid_collection_status_is_rejected():
    with pytest.raises(Exception):
        repo.insert_collection_log("2026-08-08", "kinda-ok", "scheduler",
                                    0, 0, None, 0)


def test_fetch_error_is_recorded():
    repo.log_fetch_error("2026-08-08", "HND", "SAN", "2026-12-20",
                          stops=1, via_airport=None, error_message="boom")
    with connection.get_connection() as conn:
        rows = conn.execute("SELECT * FROM fetch_errors").fetchall()
    assert len(rows) == 1
    assert rows[0]["error_message"] == "boom"


# ============================================================
# 上限（Q4）
# ============================================================

def test_per_ip_counters():
    rid = repo.upsert_monitored_route("KIX", "SFO", "2026-12-20")
    for _ in range(3):
        repo.create_watch("KIX", "SFO", "2026-12-20", [rid], ip_hash="abc")
    repo.create_watch("KIX", "SFO", "2026-12-20", [rid], ip_hash="xyz")

    since = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    assert repo.count_watches_created_since("abc", since) == 3
    assert repo.count_active_watches_for_ip("abc") == 3
    assert repo.count_active_watches_for_ip("xyz") == 1


def test_the_nearby_rule_is_off_by_default():
    """**既定はオフ（2026-08-14 変更）。**

    1モニターが最大4ルートに展開され、ルート枠の消費が利用者数の 2.4倍に
    なっていた。利用者由来のルート増加が 47本/日 に達し、上限まで6日まで
    詰まったため既定を変えた。機能自体は残し、設定タブで各自が有効にする。
    """
    assert expand_routes("SAN", "HND") == [("SAN", "HND")]
    assert expand_routes("HND", "SAN") == [("HND", "SAN")]
    # 明示的に有効にすれば従来どおり
    assert len(expand_routes("SAN", "HND", nearby_airports=True)) == 4


def test_an_existing_watch_keeps_its_own_setting():
    """既定を変えても、すでに有効にしているモニターは影響を受けない。

    黙って監視区間を減らすと、利用者は理由も分からずデータを失う。
    """
    from app.db import repository as repo

    departure = "2026-12-01"
    ids = [repo.upsert_monitored_route(o, d, departure)
           for o, d in expand_routes("SAN", "HND", nearby_airports=True)]
    watch = repo.create_watch("SAN", "HND", departure, ids, nearby_airports=True)

    assert repo.get_watch_by_token(watch.token).nearby_airports is True
    assert len(repo.routes_for_watch(watch.token)) == 4


# ======================================================================
# 調査用モニター（purpose='research'）
# ======================================================================

def test_purpose_defaults_to_user():
    rid = repo.upsert_monitored_route("NRT", "LAX", "2026-12-01")
    watch = repo.create_watch("NRT", "LAX", "2026-12-01", [rid])

    assert watch.purpose == "user"
    assert repo.get_watch_by_token(watch.token).purpose == "user"


def test_purpose_round_trips():
    rid = repo.upsert_monitored_route("SAN", "HND", "2026-12-01")
    watch = repo.create_watch("SAN", "HND", "2026-12-01", [rid],
                                    purpose="research")

    assert repo.get_watch_by_token(watch.token).purpose == "research"


def test_an_unknown_purpose_is_rejected():
    """**スキーマの CHECK で弾く。** 綴り間違いを黙って通すと、KPI から
    外れも数えられもしない中間状態のモニターができる。"""
    import sqlite3

    rid = repo.upsert_monitored_route("SAN", "HND", "2026-12-01")
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_watch("SAN", "HND", "2026-12-01", [rid],
                                purpose="reserch")   # 綴り違い


def test_a_research_monitor_survives_until_departure():
    """**出発日まで生き残ること。** 60日で失効すると、3〜6ヶ月先を狙った
    調査が出発を迎える前に打ち切られ、系列が完成しない。"""
    far = (date.today() + timedelta(days=180)).isoformat()
    rid = repo.upsert_monitored_route("SAN", "HND", far)
    watch = repo.create_watch("SAN", "HND", far, [rid], purpose="research")

    assert watch.expires_at > far, f"出発 {far} より前に失効する: {watch.expires_at}"
