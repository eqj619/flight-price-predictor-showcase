"""往復対応・段階A — スキーマと、片道が壊れないことの検証。

**このファイルの主題は「往復が動くこと」ではなく「片道が壊れないこと」。**
往復の行が同じテーブルに入るので、区間で絞る問い合わせが return_date を
指定し忘れると、片道の系列に金額がほぼ2倍の行が混ざる。混ざっても例外は
出ず、較正の数字が静かにずれるだけなので、テストで押さえる以外にない。
"""
import sqlite3

import pytest

from app.db import connection, repository as repo


def _snap(check_date, price, return_date="", airline="ANA", **kw):
    return repo.Snapshot(
        check_date=check_date, departure_date="2026-11-10",
        origin="NRT", destination="LAX", stops=0, via_airport=None,
        airline=airline, price_usd=price, source="real",
        fetched_at="2026-08-19T00:00", return_date=return_date, **kw)


# ============================================================
# 片道と往復が別の行として共存できるか
# ============================================================

def test_one_way_and_round_trip_are_separate_rows():
    """**UNIQUE に return_date が入っていないと、ここで1行に潰れる。**

    実測値を使っている。NRT->LAX 11/10 の ANA は片道 $2,164、同じ日程の
    往復が $2,226 だった（2026-08-19 実測）。金額が近いので、混ざっても
    「明らかにおかしい」とは気づけない。
    """
    repo.upsert_snapshots([_snap("2026-08-19", 2164.0),
                           _snap("2026-08-19", 2226.0, "2026-11-20")])

    one_way = repo.history_for("NRT", "LAX", "2026-11-10")
    round_trip = repo.history_for("NRT", "LAX", "2026-11-10",
                                  return_date="2026-11-20")

    assert [r["price_usd"] for r in one_way] == [2164.0]
    assert [r["price_usd"] for r in round_trip] == [2226.0]


def test_round_trip_routes_do_not_collapse_into_the_one_way_route():
    one_way = repo.upsert_monitored_route("NRT", "LAX", "2026-11-10")
    round_trip = repo.upsert_monitored_route("NRT", "LAX", "2026-11-10",
                                             "2026-11-20")
    assert one_way != round_trip
    assert len(repo.active_routes()) == 2


def test_price_history_keeps_the_two_apart():
    repo.save_price_history("NRT", "LAX", "2026-11-10",
                            [("2026-08-01", 900.0)])
    repo.save_price_history("NRT", "LAX", "2026-11-10",
                            [("2026-08-01", 1500.0)], return_date="2026-11-20")

    assert repo.price_history("NRT", "LAX", "2026-11-10")[0]["price_usd"] == 900.0
    assert repo.price_history("NRT", "LAX", "2026-11-10",
                              "2026-11-20")[0]["price_usd"] == 1500.0


# ============================================================
# 既存の片道の問い合わせに往復が混ざらないか
#
# **1関数でも return_date を落とすと、ここが落ちる。**
# ============================================================

def test_no_read_path_leaks_round_trip_rows_into_one_way():
    repo.upsert_snapshots([
        _snap("2026-08-18", 600.0), _snap("2026-08-19", 610.0),
        # 同じ区間・同じ出発日の往復。金額は倍以上に離してある。
        _snap("2026-08-18", 1580.0, "2026-11-20", airline="ZIPAIR"),
        _snap("2026-08-19", 1590.0, "2026-11-20", airline="ZIPAIR"),
    ])

    prices = [r["price_usd"] for r in repo.history_for("NRT", "LAX", "2026-11-10")]
    assert prices == [600.0, 610.0], "history_for に往復が混ざった"

    prices = [r["price_usd"] for r in repo.route_history("NRT", "LAX")]
    assert max(prices) < 1000, "route_history に往復が混ざった"

    airlines = repo.airlines_on_route("NRT", "LAX")
    assert airlines == ["ANA"], f"airlines_on_route に往復が混ざった: {airlines}"


def test_long_itinerary_count_is_scoped_to_one_way():
    repo.upsert_snapshots([
        _snap("2026-08-19", 600.0, total_minutes=700),
        # 往復側だけが上限を超える。片道の件数に数えてはいけない。
        _snap("2026-08-19", 1580.0, "2026-11-20", airline="ZIPAIR",
              total_minutes=2000),
    ])
    assert repo.count_long_itineraries("NRT", "LAX", "2026-11-10", 1000) == 0
    assert repo.count_long_itineraries("NRT", "LAX", "2026-11-10", 1000,
                                       return_date="2026-11-20") == 1


def test_backfill_targets_track_the_two_separately():
    """往復ルートは、片道の履歴があっても「未取得」として残る。"""
    repo.upsert_monitored_route("NRT", "LAX", "2036-11-10")
    repo.upsert_monitored_route("NRT", "LAX", "2036-11-10", "2036-11-20")
    repo.save_price_history("NRT", "LAX", "2036-11-10", [("2026-08-01", 900.0)])

    pending = repo.routes_without_history()
    assert [(r["departure_date"], r["return_date"]) for r in pending] \
        == [("2036-11-10", "2036-11-20")]


# ============================================================
# 既存 DB の移行
# ============================================================

# 変更前のスキーマ。**git から取らない。**
# `git show HEAD:app/db/schema.sql` で取る書き方にしていたが、この変更を
# commit した瞬間に HEAD が新スキーマになり、テストが「旧を旧のまま」
# 検証できなくなる。移行のテストは移行前の形を自分で持つ必要がある。
#
# 必要なのは return_date を持たない定義だけなので、関係する4テーブルと
# 参照元の watches / watch_routes に絞って写してある。
_OLD_SCHEMA = """
CREATE TABLE monitored_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL, destination TEXT NOT NULL,
    departure_date TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL, last_collected_at TEXT,
    disabled_at TEXT, disabled_reason TEXT,
    UNIQUE (origin, destination, departure_date));
CREATE TABLE watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL UNIQUE,
    origin TEXT NOT NULL, destination TEXT NOT NULL,
    departure_date TEXT NOT NULL, airline_filter TEXT NOT NULL DEFAULT '[]',
    max_total_minutes INTEGER, nearby_airports INTEGER NOT NULL DEFAULT 0,
    purpose TEXT NOT NULL DEFAULT 'user', notify_email TEXT,
    created_from_ip_hash TEXT, created_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE watch_routes (
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    monitored_route_id INTEGER NOT NULL REFERENCES monitored_routes(id) ON DELETE CASCADE,
    PRIMARY KEY (watch_id, monitored_route_id));
CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, check_date TEXT NOT NULL,
    departure_date TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
    stops INTEGER NOT NULL, via_airport TEXT NOT NULL DEFAULT '',
    airline TEXT NOT NULL, price_usd REAL NOT NULL,
    departure_time TEXT, arrival_time TEXT, air_minutes INTEGER,
    total_minutes INTEGER, plane_types TEXT, carbon_g INTEGER,
    source TEXT NOT NULL CHECK (source IN ('real','simulated')),
    fetched_at TEXT NOT NULL,
    UNIQUE (check_date, departure_date, origin, destination, stops, via_airport, airline));
CREATE TABLE prediction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, predicted_on TEXT NOT NULL,
    target_check_date TEXT NOT NULL, departure_date TEXT NOT NULL,
    origin TEXT NOT NULL, destination TEXT NOT NULL, stops INTEGER NOT NULL,
    via_airport TEXT NOT NULL DEFAULT '', airline TEXT NOT NULL,
    predicted_usd REAL NOT NULL, lower_usd REAL NOT NULL, upper_usd REAL NOT NULL,
    observed_points INTEGER NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (predicted_on, target_check_date, departure_date, origin, destination,
            stops, via_airport, airline));
CREATE TABLE price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT NOT NULL,
    destination TEXT NOT NULL, departure_date TEXT NOT NULL,
    observed_date TEXT NOT NULL, price_usd REAL NOT NULL, captured_at TEXT NOT NULL,
    UNIQUE (origin, destination, departure_date, observed_date));
"""


@pytest.fixture
def old_schema_db(tmp_path):
    """return_date を持たない、変更前のスキーマの DB を作る。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO watches(token,origin,destination,departure_date,"
        "created_at,last_accessed_at,expires_at) VALUES"
        "('t1','NRT','LAX','2026-11-10','2026-08-01','2026-08-19','2026-12-01')")
    conn.execute(
        "INSERT INTO monitored_routes(origin,destination,departure_date,"
        "first_seen_at) VALUES('NRT','LAX','2026-11-10','2026-08-01')")
    conn.execute("INSERT INTO watch_routes(watch_id,monitored_route_id) VALUES(1,1)")
    for day in range(1, 21):
        conn.execute(
            "INSERT INTO snapshots(check_date,departure_date,origin,destination,"
            "stops,via_airport,airline,price_usd,source,fetched_at) VALUES"
            f"('2026-08-{day:02d}','2026-11-10','NRT','LAX',0,'','ANA',"
            f"{600 + day},'real','2026-08-19T00:00')")
    conn.execute(
        "INSERT INTO price_history(origin,destination,departure_date,"
        "observed_date,price_usd,captured_at) VALUES"
        "('NRT','LAX','2026-11-10','2026-08-01',900.0,'2026-08-19T00:00')")
    conn.commit()
    conn.close()
    return path


def test_migration_loses_nothing_and_marks_everything_one_way(
        old_schema_db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("DB_PATH", str(old_schema_db))
    get_settings.cache_clear()

    assert connection._rebuild_for_return_date() == [
        "monitored_routes", "snapshots", "prediction_log", "price_history"]
    connection.init_db()

    conn = sqlite3.connect(old_schema_db)
    conn.row_factory = sqlite3.Row
    counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in ("watches", "monitored_routes", "watch_routes",
                        "snapshots", "price_history")}
    assert counts == {"watches": 1, "monitored_routes": 1, "watch_routes": 1,
                      "snapshots": 20, "price_history": 1}

    # 既存の行はすべて片道。
    for table in ("monitored_routes", "snapshots", "price_history", "watches"):
        total = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        one_way = conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE return_date = ''").fetchone()["c"]
        assert one_way == total, f"{table} に片道でない行がある"

    # 外部キーが生きている（monitored_routes は watch_routes から参照される）。
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute(
        "SELECT COUNT(*) c FROM watch_routes wr "
        "JOIN monitored_routes r ON r.id = wr.monitored_route_id"
    ).fetchone()["c"] == 1
    conn.close()


def test_migration_is_idempotent(old_schema_db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("DB_PATH", str(old_schema_db))
    get_settings.cache_clear()

    connection.init_db()
    assert connection._rebuild_for_return_date() == []
    connection.init_db()        # 2回目でも壊れない

    conn = sqlite3.connect(old_schema_db)
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 20
    conn.close()


def test_migration_restores_the_indexes(old_schema_db, monkeypatch):
    """再構築でテーブルごと落ちるので、インデックスが戻らないと全表走査になる。"""
    from app.config import get_settings
    monkeypatch.setenv("DB_PATH", str(old_schema_db))
    get_settings.cache_clear()
    connection.init_db()

    conn = sqlite3.connect(old_schema_db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name NOT LIKE 'sqlite_%'")}
    conn.close()
    assert {"idx_snapshots_route_date", "idx_routes_active",
            "idx_price_history_route", "idx_prediction_target"} <= names
