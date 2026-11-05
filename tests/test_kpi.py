"""日次 KPI 記録のテスト。

守りたいのは**履歴が壊れないこと**。KPI は時系列にして初めて意味を持つので、
列がずれる・日付が重複する・途中で切れたファイルが残る、のどれが起きても
それまでに貯めた分がまとめて価値を失う。
"""
from datetime import date, datetime, timedelta

import pytest

from app import kpi
from app.config import get_settings
from app.db import repository
from app.routing import expand_routes


TODAY = date(2026, 8, 10)


@pytest.fixture
def csv_path(tmp_path, monkeypatch):
    path = tmp_path / "kpi_daily.csv"
    monkeypatch.setenv("KPI_CSV_PATH", str(path))
    monkeypatch.setenv("KPI_S3_BUCKET", "")
    get_settings.cache_clear()
    return path


def _watch(origin="NRT", destination="SAN", departure="2026-12-01", ip_hash="h1"):
    route_ids = [
        repository.upsert_monitored_route(o, d, departure)
        for o, d in expand_routes(origin, destination)
    ]
    return repository.create_watch(origin, destination, departure, route_ids,
                                   ip_hash=ip_hash)


def test_row_has_every_column_and_date_first(csv_path):
    row = kpi.collect_kpi(TODAY)
    assert set(row) == set(kpi.COLUMNS)
    assert kpi.COLUMNS[0] == "date"
    assert row["date"] == "2026-08-10"


def test_header_is_english_only(csv_path):
    """CSV は表計算や pandas に直接食わせる。ヘッダに非 ASCII を混ぜない。"""
    kpi.append_row(kpi.collect_kpi(TODAY), csv_path)
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert header.isascii()
    assert header.split(",") == kpi.COLUMNS


def test_counts_reflect_the_database(csv_path):
    _watch(ip_hash="h1")
    _watch(destination="LAX", ip_hash="h1")
    _watch(destination="SFO", ip_hash="h2")

    row = kpi.collect_kpi(TODAY)
    assert row["watches_total"] == 3
    # 「いま何人が使っているか」の代理。同じ IP の3件を3人と数えない。
    assert row["distinct_creators"] == 2
    assert row["routes_active"] >= 3
    assert row["routes_capacity"] == get_settings().max_active_routes


def test_utilization_tracks_capacity(csv_path, monkeypatch):
    monkeypatch.setenv("MAX_ACTIVE_ROUTES", "10")
    get_settings.cache_clear()
    _watch()
    row = kpi.collect_kpi(TODAY)
    assert row["routes_utilization_pct"] == pytest.approx(
        100.0 * row["routes_active"] / 10, abs=0.01)


def test_same_date_replaces_instead_of_duplicating(csv_path):
    """日次ジョブの再実行で行が二重にならない。重複すると稼働日数が狂う。"""
    kpi.append_row(kpi.collect_kpi(TODAY), csv_path)
    _watch()
    kpi.append_row(kpi.collect_kpi(TODAY), csv_path)

    rows = kpi._read_rows(csv_path)
    assert len(rows) == 1
    assert int(rows[0]["watches_total"]) == 1   # 後から書いたほうが残る


def test_rows_stay_sorted_by_date(csv_path):
    for offset in (2, 0, 1):
        kpi.append_row(kpi.collect_kpi(TODAY + timedelta(days=offset)), csv_path)
    dates = [r["date"] for r in kpi._read_rows(csv_path)]
    assert dates == sorted(dates)


def test_collection_status_is_blank_when_no_run(csv_path):
    """収集が走らなかった日を 'success' や 0 で埋めない。"""
    assert kpi.collect_kpi(TODAY)["collection_status"] == ""


def test_collection_status_and_totals_come_from_logs(csv_path):
    repository.insert_collection_log(check_date=TODAY.isoformat(), status="partial",
                                     source="manual", routes_checked=7,
                                     records_saved=31, errors=["boom"],
                                     duration_ms=4500)
    row = kpi.collect_kpi(TODAY)
    assert row["collection_status"] == "partial"
    assert row["routes_checked"] == 7
    assert row["records_collected"] == 31
    assert row["collection_duration_seconds"] == 4.5


def test_upload_is_skipped_without_a_bucket(csv_path):
    kpi.append_row(kpi.collect_kpi(TODAY), csv_path)
    assert kpi.upload_to_s3(csv_path) is None


def test_upload_failure_never_raises(csv_path, monkeypatch):
    """S3 が落ちていても価格収集の失敗にしない。"""
    monkeypatch.setenv("KPI_S3_BUCKET", "does-not-exist")
    get_settings.cache_clear()
    kpi.append_row(kpi.collect_kpi(TODAY), csv_path)

    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "boto3":
            raise RuntimeError("no boto3")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert kpi.upload_to_s3(csv_path) is None


def test_daily_job_records_kpi(csv_path):
    """KPI は収集の後に書く。前だと当日の収集結果が空のまま残る。"""
    from app.jobs.daily import run_daily
    from tests.test_lifecycle import FakeProvider

    _watch()
    summary = run_daily(provider=FakeProvider(), today=TODAY)

    assert "kpi" not in summary["failed_steps"]
    assert summary["kpi"]["csv_path"] == str(csv_path)
    row = kpi._read_rows(csv_path)[0]
    assert row["date"] == "2026-08-10"
    assert row["collection_status"] != ""



# ======================================================================
# 定着（作られただけか、使われているか）
#
# 2026-08-16 に足した。それまでの列は登録の勢いしか映しておらず、110件の
# モニターが一度も開き直されていなくても KPI は好調に見えていた。
# ======================================================================

def _set_access(token, when):
    from app.db.connection import get_connection
    with get_connection() as conn:
        conn.execute("UPDATE watches SET last_accessed_at = ? WHERE token = ?",
                     (when, token))


def test_a_freshly_created_watch_counts_as_never_reopened(csv_path):
    """**作成直後の初回表示を再訪と数えない。**

    created_at と last_accessed_at には作成時に同じ値が入り、直後に
    ブラウザが GET を投げて数秒だけ進む。猶予を置かないと全件が
    「見られている」ことになり、この列が何も言わなくなる。
    """
    watch = _watch()
    _set_access(watch.token,
                (datetime.fromisoformat(watch.created_at)
                 + timedelta(seconds=5)).isoformat(timespec="seconds"))

    row = kpi.collect_kpi(TODAY)

    assert row["watches_never_reopened"] == 1


def _set_created(token, when):
    """**last_accessed は created 以降にしかならない。** 戻ってきた状態を
    作るには作成時刻のほうを過去へずらす。逆にすると、あり得ない状態
    （アクセスが作成より前）を検証してしまう。"""
    from app.db.connection import get_connection
    with get_connection() as conn:
        conn.execute("UPDATE watches SET created_at = ? WHERE token = ?",
                     (when, token))


def test_a_watch_opened_the_next_day_is_not_counted_as_never_reopened(csv_path):
    watch = _watch()
    now = datetime.now()
    _set_created(watch.token, (now - timedelta(days=2)).isoformat(timespec="seconds"))
    _set_access(watch.token, (now - timedelta(hours=2)).isoformat(timespec="seconds"))

    row = kpi.collect_kpi(TODAY)

    assert row["watches_never_reopened"] == 0
    assert row["watches_accessed_last_24h"] == 1


def test_access_windows_separate_recent_from_stale(csv_path):
    """24時間・7日・それ以前を区別できること。"""
    recent = _watch("NRT", "LAX")
    week = _watch("HND", "SFO")
    old = _watch("SEA", "NRT")
    now = datetime.now()
    for w, when in ((recent, timedelta(hours=2)), (week, timedelta(days=3)),
                    (old, timedelta(days=30))):
        _set_created(w.token, (now - when - timedelta(days=1)).isoformat(timespec="seconds"))
        _set_access(w.token, (now - when).isoformat(timespec="seconds"))

    row = kpi.collect_kpi(TODAY)

    assert row["watches_accessed_last_24h"] == 1
    assert row["watches_accessed_last_7d"] == 2      # 直近24時間ぶんを含む
    assert row["watches_total"] == 3


def test_the_new_columns_reach_the_csv(csv_path):
    """列を足しても書き出しが揃っていること。"""
    _watch()
    kpi.record_daily(TODAY)

    written = csv_path.read_text(encoding="utf-8").splitlines()
    header = written[0].split(",")

    for column in ("watches_accessed_last_24h", "watches_accessed_last_7d",
                   "watches_never_reopened"):
        assert column in header
    assert len(written[1].split(",")) == len(header)


# ======================================================================
# 調査用モニターを需要・定着の列から外す
#
# 2026-08-17、価格曲線を調べるために120件のモニターを入れた。区別しないと
# 「作られたきり開かれていないモニターが何件あるか」（前日 73/110）が
# 自分たちの分で埋まり、利用者が使っているかを測れなくなる。
# ======================================================================

def _research_watch(origin="SAN", destination="HND", departure="2026-12-01"):
    rid = repository.upsert_monitored_route(origin, destination, departure)
    return repository.create_watch(origin, destination, departure, [rid],
                                   nearby_airports=False, purpose="research")


def test_research_monitors_are_excluded_from_demand(csv_path):
    _watch()                      # 利用者のもの
    _research_watch()
    _research_watch("HND", "SAN")

    row = kpi.collect_kpi(TODAY)

    assert row["watches_total"] == 1, "利用者のモニターだけを数えること"
    assert row["watches_active"] == 1
    assert row["watches_research"] == 2


def test_research_monitors_are_excluded_from_retention(csv_path):
    """**ここが本題。** 調査用が混ざると定着の指標が読めなくなる。"""
    _research_watch()
    _research_watch("HND", "SAN")
    _research_watch("SAN", "NRT")

    row = kpi.collect_kpi(TODAY)

    assert row["watches_never_reopened"] == 0, "調査用を『開かれていない』に数えない"
    assert row["watches_accessed_last_24h"] == 0
    assert row["watches_research"] == 3


def test_research_monitors_still_count_toward_capacity(csv_path):
    """**容量には数える。** ルートを消費し収集時間を使うのは同じ。"""
    _research_watch()

    row = kpi.collect_kpi(TODAY)

    assert row["routes_active"] == 1
    assert row["watches_total"] == 0


def test_research_monitors_do_not_hide_in_the_creator_count(csv_path):
    _watch(ip_hash="user1")
    _research_watch()

    row = kpi.collect_kpi(TODAY)

    assert row["distinct_creators"] == 1


def test_existing_watches_default_to_user(csv_path):
    """**既存行は 'user' になる。** 列を足す前に作られたものは利用者のもの。"""
    watch = _watch()

    assert watch.purpose == "user"
    assert kpi.collect_kpi(TODAY)["watches_total"] == 1


# ============================================================
# 通知の列（課題4。2026-08-22 追加）
# ============================================================

def _push_watch_with_subscriber(endpoint="https://push.example/aaaaaaaaaaaaaaaa"):
    from datetime import date, timedelta
    from app.db import repository as repo
    dep = (date.today() + timedelta(days=100)).isoformat()
    route_id = repo.upsert_monitored_route("NRT", "LAX", dep)
    watch = repo.create_watch(origin="NRT", destination="LAX",
                              departure_date=dep, route_ids=[route_id])
    sid = repo.upsert_push_subscription(endpoint, "BKey0000", "auth0000")
    repo.link_push_subscription(sid, watch.token)
    return watch


def test_kpi_counts_push_subscriptions():
    from app import kpi
    _push_watch_with_subscriber()
    row = kpi.collect_kpi()
    assert row["push_subscriptions"] == 1
    assert row["push_watches"] == 1


def test_kpi_reports_zero_when_nothing_is_subscribed():
    """**空欄ではなく 0。** 記録が無いのと 0 件は違う。"""
    from app import kpi
    row = kpi.collect_kpi()
    assert row["push_subscriptions"] == 0
    assert row["push_sent_last_24h"] == 0


def test_kpi_counts_what_was_actually_sent():
    """**送信数は notification_logs からしか出せない。**

    watches.notified_rise_at は抑制したときにも立つので送信数にならず、
    notified_low_usd は日付を持たない。
    """
    from datetime import date
    from app import kpi
    from app.db import repository as repo

    today = date.today().isoformat()
    repo.insert_notification_log(today, {"checked": 3, "low": 1, "rise": 1,
                                         "sent": 2, "gone": 1, "failed": 0})
    row = kpi.collect_kpi()
    assert row["push_sent_last_24h"] == 2
    assert row["push_gone_last_24h"] == 1


def test_kpi_sums_several_runs_on_the_same_day():
    """手で走らせることがあるので、同じ日の記録は合算する。"""
    from datetime import date
    from app import kpi
    from app.db import repository as repo

    today = date.today().isoformat()
    repo.insert_notification_log(today, {"sent": 1, "gone": 0})
    repo.insert_notification_log(today, {"sent": 2, "gone": 1})
    row = kpi.collect_kpi()
    assert row["push_sent_last_24h"] == 3
    assert row["push_gone_last_24h"] == 1


def test_the_notification_run_is_logged_even_when_nothing_is_sent():
    """**0件の日も記録する。** 記録が無い日と区別できないと、止まって
    いたことに気づけない。"""
    from datetime import date
    from app import notify
    from app.config import get_settings
    from app.db import repository as repo
    import os

    os.environ["VAPID_PUBLIC_KEY"] = "BTest"
    os.environ["VAPID_PRIVATE_KEY"] = "test"
    get_settings.cache_clear()
    try:
        notify.run(date.today(), sender=lambda *a: "sent")
        from app.db.connection import get_connection
        with get_connection() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM notification_logs").fetchone()["c"]
        assert n == 1
    finally:
        os.environ.pop("VAPID_PUBLIC_KEY", None)
        os.environ.pop("VAPID_PRIVATE_KEY", None)
        get_settings.cache_clear()


# これまでに出荷した列の順序。**ここは二度と変えない。**
# 途中に挿す・並べ替える・消すと、S3 に積んだ過去の行と列がずれる。
# 新しい列はこの後ろに足す（このリストは伸ばさない）。
_SHIPPED_COLUMNS = [
    "date",
    "watches_total", "watches_active", "watches_created_last_24h",
    "distinct_creators",
    "watches_accessed_last_24h", "watches_accessed_last_7d",
    "watches_never_reopened", "watches_research",
    "routes_active", "routes_capacity", "routes_utilization_pct",
    "collection_status", "routes_checked", "records_collected",
    "collection_duration_seconds", "fetch_errors",
    "snapshots_real", "snapshots_simulated", "observed_days",
    "series_total", "series_completed", "distinct_markets",
    "distinct_departure_dates", "distinct_airlines", "predictions_logged",
    "db_size_bytes",
    "push_subscriptions", "push_watches", "push_sent_last_24h",
    "push_gone_last_24h",
]


def test_columns_are_only_ever_appended():
    """**列は末尾にしか足さない。** 途中に挿すと過去の行と列がずれる。

    末尾N列を固定するのではなく、**出荷済みの並びが前方一致で残って
    いること**を見る。こうしておけば、列を足すたびにこの試験を書き直す
    必要が無い（書き直す運用だと、うっかり並べ替えても試験ごと直って
    しまい、検知にならない）。
    """
    from app import kpi
    assert kpi.COLUMNS[:len(_SHIPPED_COLUMNS)] == _SHIPPED_COLUMNS


def test_every_column_is_produced():
    """宣言した列が全部埋まること。抜けると CSV に空欄が並ぶ。"""
    from app import kpi
    row = kpi.collect_kpi()
    missing = [c for c in kpi.COLUMNS if c not in row]
    assert not missing, f"値を作っていない列: {missing}"


# ── 列を足したときに、それ以前の行を失わない ────────────────────
#
# CSV は毎回まるごと書き直している（_to_csv が COLUMNS を見出しにする）。
# **列を足した日に、前日までの行が消えないことを確かめる。** 消えると
# 推移のグラフが履歴ごと失われ、しかも翌朝まで気づけない。

def test_adding_a_column_keeps_the_older_rows(csv_path):
    """裾（P90）の4列を足した 2026-09-10 の状況を再現する。"""
    from app import kpi
    old_columns = [c for c in kpi.COLUMNS if not c.endswith("p90_pct")
                   and not c.endswith("p90_pct_near")]
    header = ",".join(old_columns)
    csv_path.write_text(
        f"{header}\n"
        + ",".join("2026-09-08" if c == "date" else "1" for c in old_columns) + "\n"
        + ",".join("2026-09-09" if c == "date" else "2" for c in old_columns) + "\n",
        encoding="utf-8")

    kpi.append_row({"date": "2026-09-10",
                    "prediction_abs_error_p90_pct": 18.4,
                    "prediction_bias_p90_pct": 15.1}, csv_path)

    import csv as _csv
    rows = list(_csv.DictReader(csv_path.open(newline="", encoding="utf-8")))
    assert [r["date"] for r in rows] == ["2026-09-08", "2026-09-09", "2026-09-10"]
    # 足す前の日は空欄。**0 で埋めない** — 「誤差0%」に見える。
    assert rows[0]["prediction_abs_error_p90_pct"] == ""
    assert rows[2]["prediction_abs_error_p90_pct"] == "18.4"
    assert rows[2]["prediction_bias_p90_pct"] == "15.1"


def test_the_tail_columns_reach_the_csv(csv_path):
    """測った値が CSV まで来ること。**測っても運ばなければ意味がない。**"""
    from app import kpi
    row = kpi.collect_kpi()
    for column in ("prediction_abs_error_p90_pct", "prediction_bias_p90_pct",
                   "prediction_abs_error_p90_pct_near",
                   "prediction_bias_p90_pct_near"):
        assert column in row, column
