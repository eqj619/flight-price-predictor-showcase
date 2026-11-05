"""出発時刻を便の身元に入れる（2026-09-08）。

**同じ社・同じ経由地で1日に何本も飛ぶ。** SAN→HND の United+ANA・LAX 経由は
06:15 と 17:00 の2本あった。UNIQUE キーが (経由回数, 経由地, 航空会社) までしか
無かったので2本が1行を奪い合い、`_cheapest_distinct` が安いほうを選んだ結果、
**日によって中身の違う便が1本の線につながっていた** — 8/23〜8/27 に $688 で
出ていたのは、それ以前の $588 とは別の便だった。

ここで見るのは3つ。

1. 移行で1行も失わないこと（既存の実測は遡って取り直せない）
2. NULL が '' になること（SQLite の UNIQUE は NULL != NULL なので、
   NULL のまま鍵に入れると重複排除が黙って壊れる）
3. 移行のあと、時刻の違う2便が別々に入ること
"""
import sqlite3

import pytest

from app.db import connection, repository

# 変更前のスキーマ（return_date はあるが departure_time は NULL 可・鍵の外）。
_OLD_SCHEMA = """
CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_date TEXT NOT NULL, departure_date TEXT NOT NULL,
    return_date TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL, destination TEXT NOT NULL,
    stops INTEGER NOT NULL, via_airport TEXT NOT NULL DEFAULT '',
    airline TEXT NOT NULL, price_usd REAL NOT NULL,
    departure_time TEXT, arrival_time TEXT, air_minutes INTEGER,
    total_minutes INTEGER, plane_types TEXT, carbon_g INTEGER,
    source TEXT NOT NULL CHECK (source IN ('real','simulated')),
    fetched_at TEXT NOT NULL,
    UNIQUE (check_date, departure_date, return_date, origin, destination,
            stops, via_airport, airline));
CREATE TABLE prediction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    predicted_on TEXT NOT NULL, target_check_date TEXT NOT NULL,
    departure_date TEXT NOT NULL, return_date TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL, destination TEXT NOT NULL, stops INTEGER NOT NULL,
    via_airport TEXT NOT NULL DEFAULT '', airline TEXT NOT NULL,
    predicted_usd REAL NOT NULL, lower_usd REAL NOT NULL, upper_usd REAL NOT NULL,
    observed_points INTEGER NOT NULL, actual_usd REAL, scored_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (predicted_on, target_check_date, departure_date, return_date,
            origin, destination, stops, via_airport, airline));
"""


@pytest.fixture
def old_db(tmp_path, monkeypatch):
    from app.config import get_settings
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    # 時刻のある行と、取れなかった行（正時の便がこれになっていた）。
    for day, at in ((9, "2026-10-08T06:15"), (10, None), (11, "2026-10-08T06:15")):
        conn.execute(
            "INSERT INTO snapshots(check_date,departure_date,origin,destination,"
            "stops,via_airport,airline,price_usd,departure_time,source,fetched_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"2026-08-{day:02d}", "2026-10-08", "SAN", "HND", 1, "LAX",
             "United, ANA", 588.0, at, "real", "2026-08-19T00:00"))
    conn.execute(
        "INSERT INTO prediction_log(predicted_on,target_check_date,departure_date,"
        "origin,destination,stops,via_airport,airline,predicted_usd,lower_usd,"
        "upper_usd,observed_points,created_at) VALUES"
        "('2026-08-09','2026-08-16','2026-10-08','SAN','HND',1,'LAX','United, ANA',"
        "600.0,550.0,650.0,3,'2026-08-09T00:00')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("DB_PATH", str(path))
    get_settings.cache_clear()
    return path


def _rows(path, sql):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def test_the_migration_loses_nothing(old_db):
    """**実測は遡って取り直せない。** 1行でも落としたら終わり。"""
    assert connection._rebuild_for_departure_time() == [
        "snapshots", "prediction_log"]
    connection.init_db()

    assert len(_rows(old_db, "SELECT * FROM snapshots")) == 3
    assert len(_rows(old_db, "SELECT * FROM prediction_log")) == 1


def test_an_unknown_time_becomes_empty_not_null(old_db):
    """NULL を鍵に入れると、SQLite は NULL != NULL と評価して重複排除が壊れる。"""
    connection.init_db()
    times = sorted(r["departure_time"]
                   for r in _rows(old_db, "SELECT departure_time FROM snapshots"))
    assert times == ["", "2026-10-08T06:15", "2026-10-08T06:15"]
    # 予測側は、その予測が指していた便の時刻で埋め直される
    # （下の test_pending_predictions_can_still_be_scored を見よ）。
    assert _rows(old_db, "SELECT departure_time FROM prediction_log")[0][
        "departure_time"] == "2026-10-08T06:15"


def test_the_migration_is_idempotent(old_db):
    connection.init_db()
    assert connection._rebuild_for_departure_time() == []
    connection.init_db()        # 2回目でも壊れない
    assert len(_rows(old_db, "SELECT * FROM snapshots")) == 3


def test_after_the_migration_two_flights_at_different_times_coexist(old_db):
    """**これが直したかったこと。** 同じ社・同じ経由地の別便が同居する。"""
    connection.init_db()
    common = dict(check_date="2026-09-08", departure_date="2026-10-08",
                  origin="SAN", destination="HND", stops=1, via_airport="LAX",
                  airline="United, ANA", source="real",
                  fetched_at="2026-09-08T00:00")
    repository.upsert_snapshots([
        repository.Snapshot(**common, price_usd=576.0,
                            departure_time="2026-10-08T06:15"),
        repository.Snapshot(**common, price_usd=676.0,
                            departure_time="2026-10-08T17:00"),
    ])
    got = {r["departure_time"]: r["price_usd"] for r in _rows(
        old_db, "SELECT departure_time, price_usd FROM snapshots "
                "WHERE check_date='2026-09-08'")}
    assert got == {"2026-10-08T06:15": 576.0, "2026-10-08T17:00": 676.0}


def test_the_same_flight_recollected_is_still_one_row(old_db):
    """鍵が広がっても、同じ便の取り直しは上書きであって追加ではない。"""
    connection.init_db()
    common = dict(check_date="2026-09-08", departure_date="2026-10-08",
                  origin="SAN", destination="HND", stops=1, via_airport="LAX",
                  airline="United, ANA", source="real",
                  departure_time="2026-10-08T06:15", fetched_at="2026-09-08T00:00")
    repository.upsert_snapshots([repository.Snapshot(**common, price_usd=576.0)])
    repository.upsert_snapshots([repository.Snapshot(**common, price_usd=540.0)])
    rows = _rows(old_db, "SELECT price_usd FROM snapshots "
                         "WHERE check_date='2026-09-08'")
    assert [r["price_usd"] for r in rows] == [540.0]


def test_scoring_matches_the_flight_not_just_the_airline(old_db):
    """**答え合わせは同じ便で。** 別の便の値段を拾えば精度の数字が嘘になる。"""
    connection.init_db()
    common = dict(check_date="2026-09-08", departure_date="2026-10-08",
                  origin="SAN", destination="HND", stops=1, via_airport="LAX",
                  airline="United, ANA", source="real",
                  fetched_at="2026-09-08T00:00")
    repository.upsert_snapshots([
        repository.Snapshot(**common, price_usd=576.0,
                            departure_time="2026-10-08T06:15"),
        repository.Snapshot(**common, price_usd=676.0,
                            departure_time="2026-10-08T17:00"),
    ])
    repository.insert_predictions([repository.Prediction(
        predicted_on="2026-09-01", target_check_date="2026-09-08",
        departure_date="2026-10-08", origin="SAN", destination="HND",
        stops=1, via_airport="LAX", airline="United, ANA",
        departure_time="2026-10-08T17:00",
        predicted_usd=700.0, lower_usd=650.0, upper_usd=750.0,
        observed_points=5)])

    repository.score_predictions("2026-09-08")

    scored = [r for r in _rows(old_db, "SELECT * FROM prediction_log")
              if r["actual_usd"] is not None]
    assert len(scored) == 1
    assert scored[0]["actual_usd"] == 676.0      # 17:00 の便。576.0 ではない


def test_pending_predictions_can_still_be_scored(old_db):
    """**移行で採点待ちの予測を殺さないこと。**

    departure_time を突き合わせの条件に足したので、予測側が '' のままだと
    実時刻を持つスナップショットと二度と一致しない。移行の時点で
    prediction_log は 39,035 件あり、うち 26,000 件ほどが未採点だった。

    埋め方は一意に決まる。移行前は1日・1便あたり1行しか持てなかったので、
    predicted_on の日の同じ (経由回数, 経由地, 航空会社) のスナップショットは
    高々1行。その出発時刻がその予測の便の時刻になる。
    """
    connection.init_db()

    # 対象日の実測が入る（新しい鍵なので、時刻の違う2便が並ぶ）。
    common = dict(check_date="2026-08-16", departure_date="2026-10-08",
                  origin="SAN", destination="HND", stops=1, via_airport="LAX",
                  airline="United, ANA", source="real",
                  fetched_at="2026-08-16T00:00")
    repository.upsert_snapshots([
        repository.Snapshot(**common, price_usd=588.0,
                            departure_time="2026-10-08T06:15"),
        repository.Snapshot(**common, price_usd=688.0,
                            departure_time="2026-10-08T17:00"),
    ])

    assert repository.score_predictions("2026-08-16") == 1
    row = _rows(old_db, "SELECT * FROM prediction_log")[0]
    # 06:15 の便についての予測だったので、588.0 で採点される。
    assert row["actual_usd"] == 588.0


def test_a_prediction_with_no_matching_snapshot_is_left_alone(old_db, monkeypatch):
    """**推測して埋めない。** 間違った便で採点するくらいなら採点しない。"""
    conn = sqlite3.connect(old_db)
    conn.execute(
        "INSERT INTO prediction_log(predicted_on,target_check_date,"
        "departure_date,origin,destination,stops,via_airport,airline,"
        "predicted_usd,lower_usd,upper_usd,observed_points,created_at) VALUES"
        "('2026-08-09','2026-08-16','2026-10-08','SAN','HND',1,'SEA','Delta',"
        "900.0,850.0,950.0,3,'2026-08-09T00:00')")
    conn.commit()
    conn.close()

    connection.init_db()

    times = {(r["via_airport"], r["departure_time"])
             for r in _rows(old_db, "SELECT via_airport, departure_time "
                                    "FROM prediction_log")}
    assert times == {("LAX", "2026-10-08T06:15"), ("SEA", "")}


def test_the_backfill_is_idempotent(old_db):
    connection.init_db()
    before = _rows(old_db, "SELECT departure_time FROM prediction_log")
    connection.init_db()
    assert _rows(old_db, "SELECT departure_time FROM prediction_log") == before
