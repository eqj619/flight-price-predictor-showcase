"""ライフサイクル管理と日次ジョブのテスト。

匿名運用では「誰も見なくなったモニター」を人が消してくれない。放置された
モニターの価格を無期限に取り続けないことが、そのままコストの上限になる。
"""
from datetime import date, timedelta

import pytest

from app.collect.provider import PriceProvider, Quote
from app.config import get_settings
from app.db import repository
from app.db.connection import get_connection
from app.jobs.daily import run_daily, run_lifecycle
from app.jobs.scheduler import DailyScheduler
from app.routing import expand_routes

TODAY = date(2026, 8, 9)
FUTURE = (TODAY + timedelta(days=60)).isoformat()


class FakeProvider(PriceProvider):
    name = "fake"

    def __init__(self, price=700.0):
        self.calls = []
        self._price = price

    def fetch(self, origin, destination, departure_date, stops, airlines=None,
              return_date=None):
        self.calls.append((origin, destination, stops,
                           tuple(airlines) if airlines else None))
        if stops == 0 and airlines is None:
            return [Quote(airline="ANA", stops=0, via_airport=None,
                          price_usd=self._price)]
        return []

    @property
    def routes_called(self):
        return {(o, d) for o, d, _s, _a in self.calls}


def _register(origin, destination, departure=FUTURE):
    route_ids = [
        repository.upsert_monitored_route(o, d, departure)
        for o, d in expand_routes(origin, destination, nearby_airports=True)
    ]
    return repository.create_watch(origin, destination, departure, route_ids)


def _set_expiry(token, expires_at):
    with get_connection() as conn:
        conn.execute("UPDATE watches SET expires_at = ? WHERE token = ?",
                     (expires_at, token))


# ======================================================================
# 失効
# ======================================================================

def test_an_expired_watch_is_deleted():
    watch = _register("KIX", "SFO")
    _set_expiry(watch.token, (TODAY - timedelta(days=1)).isoformat())

    result = run_lifecycle(TODAY)

    assert result["expired_watches"] == 1
    assert repository.get_watch_by_token(watch.token) is None


def test_a_live_watch_survives():
    watch = _register("KIX", "SFO")
    run_lifecycle(TODAY)
    assert repository.get_watch_by_token(watch.token) is not None


def test_expiry_is_the_earlier_of_idle_limit_and_departure_grace():
    # 出発が近ければ、放置期限（60日）より出発日 + 7日のほうが早く来る。
    soon = (date.today() + timedelta(days=5)).isoformat()
    watch = _register("NRT", "LAX", departure=soon)
    assert watch.expires_at == (date.fromisoformat(soon) + timedelta(days=7)).isoformat()


def test_deleting_an_expired_watch_frees_its_routes():
    watch = _register("NRT", "SAN")          # 東京2空港 x 南カリフォルニア2空港
    assert repository.count_active_routes() == 4

    _set_expiry(watch.token, (TODAY - timedelta(days=1)).isoformat())
    result = run_lifecycle(TODAY)

    assert result["orphan_routes"] == 4
    assert repository.count_active_routes() == 0


def test_a_route_shared_with_a_live_watch_is_not_freed():
    keeper = _register("KIX", "SFO")
    doomed = _register("KIX", "SFO")
    _set_expiry(doomed.token, (TODAY - timedelta(days=1)).isoformat())

    run_lifecycle(TODAY)

    # まだ見ている人がいるルートを止めてはいけない。
    assert repository.count_active_routes() == 1
    assert repository.get_watch_by_token(keeper.token) is not None


def test_a_departed_route_stops_being_collected():
    past = (TODAY - timedelta(days=1)).isoformat()
    _register("KIX", "SFO", departure=past)

    result = run_lifecycle(TODAY)

    assert result["departed_routes"] >= 1
    assert repository.count_active_routes() == 0


def test_the_price_history_survives_every_kind_of_cleanup():
    watch = _register("KIX", "SFO")
    repository.upsert_snapshots([repository.Snapshot(
        check_date=TODAY.isoformat(), departure_date=FUTURE,
        origin="KIX", destination="SFO", stops=0, via_airport=None,
        airline="ANA", price_usd=700.0, source="real",
        fetched_at=TODAY.isoformat(),
    )])

    _set_expiry(watch.token, (TODAY - timedelta(days=1)).isoformat())
    run_lifecycle(TODAY)

    # 収集済みの価格は資産。同じルートが再登録されたとき、初日から
    # 過去トレンドを見せられる。
    assert repository.history_for("KIX", "SFO", FUTURE)


def test_a_reregistered_route_reactivates_and_keeps_its_history():
    watch = _register("KIX", "SFO")
    repository.upsert_snapshots([repository.Snapshot(
        check_date=TODAY.isoformat(), departure_date=FUTURE,
        origin="KIX", destination="SFO", stops=0, via_airport=None,
        airline="ANA", price_usd=700.0, source="real",
        fetched_at=TODAY.isoformat(),
    )])
    _set_expiry(watch.token, (TODAY - timedelta(days=1)).isoformat())
    run_lifecycle(TODAY)
    assert repository.count_active_routes() == 0

    _register("KIX", "SFO")

    assert repository.count_active_routes() == 1
    assert len(repository.history_for("KIX", "SFO", FUTURE)) == 1


# ======================================================================
# 日次ジョブ
# ======================================================================

def test_the_daily_job_runs_cleanup_before_collecting():
    # 今日で失効するモニターのために、今日の分をもう一度取りに行かない。
    watch = _register("KIX", "SFO")
    _set_expiry(watch.token, (TODAY - timedelta(days=1)).isoformat())
    provider = FakeProvider()

    run_daily(provider, TODAY)

    assert provider.calls == []


def test_the_daily_job_collects_stores_and_predicts():
    _register("KIX", "SFO")
    provider = FakeProvider()

    summary = run_daily(provider, TODAY)

    assert summary["collection"]["rows_written"] == 1
    assert summary["predictions_logged"] > 0
    assert summary["failed_steps"] == []


def test_two_watches_on_the_same_route_produce_one_query():
    _register("KIX", "SFO")
    _register("KIX", "SFO")     # 別ユーザーが同じ条件を登録
    provider = FakeProvider()

    run_daily(provider, TODAY)

    # 日次コストはユーザー数ではなく、異なるルート数に比例する。
    assert provider.calls.count(("KIX", "SFO", 0, None)) == 1


def test_the_daily_job_never_raises():
    _register("KIX", "SFO")

    class ExplodingProvider(PriceProvider):
        name = "exploding"

        def fetch(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    # 無人で回る前提なので、1回の失敗を人が見に来なくても翌日に戻ってくる。
    summary = run_daily(ExplodingProvider(), TODAY)
    assert summary["collection"]["status"] == "failed"


def test_one_broken_step_does_not_stop_the_others(monkeypatch):
    _register("KIX", "SFO")
    monkeypatch.setattr("app.jobs.daily.run_lifecycle",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db down")))

    summary = run_daily(FakeProvider(), TODAY)

    # 整理が落ちても収集は済ませる。
    assert summary["failed_steps"] == ["lifecycle"]
    assert summary["collection"]["rows_written"] == 1


def test_the_daily_job_is_idempotent_within_a_day():
    _register("KIX", "SFO")
    provider = FakeProvider()

    run_daily(provider, TODAY)
    run_daily(provider, TODAY)

    with get_connection() as conn:
        rows = conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
    # 同じ check_date の再実行は upsert で潰れる。
    assert rows == 1


# ======================================================================
# スケジューラ
# ======================================================================

def test_an_overlapping_run_is_skipped():
    """収集は500ルートで90分近くかかりうる。二重に走ると Google Flights への
    リクエストが倍になり、ポライトネス間隔を守っている意味が無くなる。"""
    calls = []

    def slow_job():
        calls.append("start")
        # 走っている最中に、もう一度スケジュールが来た状況を作る。
        assert scheduler.run_once() is None
        return {"ok": True}

    scheduler = DailyScheduler(job=slow_job)
    scheduler.run_once()

    assert calls == ["start"]
    assert scheduler.last_skipped_at is not None


def test_the_guard_is_released_even_if_the_job_raises():
    def boom():
        raise RuntimeError("boom")

    scheduler = DailyScheduler(job=boom)
    scheduler.run_once()

    # ここで解放し損ねると、以後すべての実行が「実行中」として黙って
    # 飛ばされ続ける。
    assert scheduler.is_running is False
    scheduler2 = DailyScheduler(job=lambda: {"ok": True})
    assert scheduler2.run_once() == {"ok": True}


def test_a_disabled_scheduler_does_not_start(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    get_settings.cache_clear()

    scheduler = DailyScheduler(job=lambda: {})
    scheduler.start()
    assert scheduler.status()["enabled"] is False
    assert scheduler.status()["next_run"] is None
    scheduler.shutdown()

    get_settings.cache_clear()


def test_a_started_scheduler_reports_its_next_run(monkeypatch):
    from app.config import get_settings

    # conftest がテスト中はスケジューラを切っているので、ここだけ戻す。
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    get_settings.cache_clear()

    scheduler = DailyScheduler(job=lambda: {})
    scheduler.start()
    try:
        status = scheduler.status()
        assert status["next_run"] is not None
        assert status["schedule"] == "03:00"
    finally:
        scheduler.shutdown()
        get_settings.cache_clear()


def test_shutdown_is_safe_to_call_twice():
    scheduler = DailyScheduler(job=lambda: {})
    scheduler.start()
    scheduler.shutdown()
    scheduler.shutdown()


# ======================================================================
# ログ
# ======================================================================

def test_the_app_logger_actually_gets_a_handler():
    import logging

    import app.logging_setup as logging_setup

    # ハンドラが付いていないと、無人で回る日次ジョブの結果も例外も
    # どこにも出ない。uvicorn 起動時に実際にこれが起きていた。
    logging_setup._configured = False
    logging.getLogger("app").handlers.clear()
    logging_setup.configure_logging()

    assert logging.getLogger("app").handlers


def test_configure_logging_is_idempotent():
    import logging

    import app.logging_setup as logging_setup

    logging_setup._configured = False
    logging.getLogger("app").handlers.clear()
    logging_setup.configure_logging()
    count = len(logging.getLogger("app").handlers)
    logging_setup.configure_logging()

    # --reload で二重に呼ばれてもログが二重に出ないこと。
    assert len(logging.getLogger("app").handlers) == count


# ======================================================================
# タイムゾーン
# ======================================================================

def test_today_follows_the_configured_timezone(monkeypatch):
    from app import clock
    from app.config import get_settings

    monkeypatch.setenv("TIMEZONE", "Asia/Tokyo")
    get_settings.cache_clear()
    tokyo = clock.today()

    monkeypatch.setenv("TIMEZONE", "Pacific/Honolulu")
    get_settings.cache_clear()
    honolulu = clock.today()

    # 東京とホノルルは19時間差なので、同時刻に同じ日付とは限らない。
    # サーバの OS タイムゾーンではなく設定に従っていることを示す。
    assert (tokyo - honolulu).days in (0, 1)

    get_settings.cache_clear()


# ======================================================================
# 系列を出発日まで走りきらせる（学習データの完全性）
# ======================================================================

def test_a_watch_survives_until_its_departure_date():
    """放置期限で系列が途中で切れないこと。

    切れた系列は「残り期間の最小値がいくらだったか」が確定せず、価格曲線の
    推定にも学習にも使えない。既定の collect_until_departure=True では
    出発日 + 猶予まで生きる。
    """
    settings = get_settings()
    far = (date.today() + timedelta(days=200)).isoformat()   # 放置期限(60)より先
    watch = _register("KIX", "SFO", departure=far)

    expected = (date.fromisoformat(far)
                + timedelta(days=settings.watch_post_departure_grace_days))
    assert watch.expires_at == expected.isoformat()


def test_the_old_idle_rule_can_be_restored(monkeypatch):
    from app.config import get_settings as gs

    monkeypatch.setenv("COLLECT_UNTIL_DEPARTURE", "false")
    gs.cache_clear()
    settings = gs()

    far = (date.today() + timedelta(days=200)).isoformat()
    watch = _register("KIX", "SFO", departure=far)

    # 仕様 2.4 当初の「早い方」に戻る。
    expected = date.today() + timedelta(days=settings.watch_idle_expiry_days)
    assert watch.expires_at == expected.isoformat()

    gs.cache_clear()


def test_a_far_future_watch_keeps_collecting_past_the_idle_limit():
    far = (date.today() + timedelta(days=200)).isoformat()
    watch = _register("KIX", "SFO", departure=far)

    # 放置期限を過ぎた日に整理を回しても消えない。
    run_lifecycle(date.today() + timedelta(days=90))

    assert repository.get_watch_by_token(watch.token) is not None
    assert repository.count_active_routes() == 1


def test_refresh_expiry_does_not_pretend_the_user_visited():
    watch = _register("KIX", "SFO")
    before = repository.get_watch_by_token(watch.token).last_accessed_at

    repository.refresh_expiry(watch.token)

    # 設定変更の反映であって、アクセスではない。
    assert repository.get_watch_by_token(watch.token).last_accessed_at == before


# ======================================================================
# エクスポート（学習・分析用）
# ======================================================================

def _seed_price(origin, destination, departure, check_date, price, airline="ANA"):
    repository.upsert_snapshots([repository.Snapshot(
        check_date=check_date, departure_date=departure,
        origin=origin, destination=destination, stops=0, via_airport=None,
        airline=airline, price_usd=price, source="real", fetched_at=check_date,
    )])


def test_export_includes_days_to_departure():
    _seed_price("KIX", "SFO", "2026-10-08", "2026-08-09", 700.0)

    row = repository.export_snapshots()[0]

    # 学習では「出発まで何日か」が主要な特徴量になる。毎回 SQL を書かせない。
    assert row["days_to_departure"] == 60
    assert row["price_usd"] == 700.0


def test_export_excludes_simulated_by_default():
    _seed_price("KIX", "SFO", "2026-10-08", "2026-08-09", 700.0)
    repository.upsert_snapshots([repository.Snapshot(
        check_date="2026-08-09", departure_date="2026-10-08",
        origin="KIX", destination="SFO", stops=0, via_airport=None,
        airline="ZIPAIR", price_usd=1.0, source="simulated",
        fetched_at="2026-08-09",
    )])

    assert {r["airline"] for r in repository.export_snapshots()} == {"ANA"}
    assert len(repository.export_snapshots(("real", "simulated"))) == 2


def test_completed_only_keeps_series_that_reached_departure():
    _seed_price("KIX", "SFO", "2026-07-01", "2026-06-01", 700.0)   # 出発済み
    _seed_price("KIX", "SEA", "2026-12-01", "2026-08-09", 800.0)   # 進行中

    done = repository.export_snapshots(completed_only=True, today="2026-08-09")

    # 進行中の系列は「残り期間の最小値」が未確定なので学習に使えない。
    assert {r["destination"] for r in done} == {"SFO"}


def test_predictions_export_joins_the_actual_price():
    repository.insert_predictions([repository.Prediction(
        predicted_on="2026-08-09", target_check_date="2026-08-10",
        departure_date="2026-10-08", origin="KIX", destination="SFO",
        stops=0, via_airport=None, airline="ANA",
        predicted_usd=700.0, lower_usd=650.0, upper_usd=750.0, observed_points=5,
    )])
    _seed_price("KIX", "SFO", "2026-10-08", "2026-08-10", 712.0)

    row = repository.export_predictions()[0]

    assert row["horizon_days"] == 1
    assert row["predicted_usd"] == 700.0
    assert row["actual_usd"] == 712.0      # 突き合わせ済み


def test_dataset_summary_counts_completed_series():
    _seed_price("KIX", "SFO", "2026-07-01", "2026-06-01", 700.0)
    _seed_price("KIX", "SEA", "2026-12-01", "2026-08-09", 800.0)

    d = repository.dataset_summary(today="2026-08-09")

    assert d["series"] == 2
    assert d["completed_series"] == 1


# ======================================================================
# 収集の健全性（恒常的な失敗に埋もれさせない）
# ======================================================================

def _log_error(check_date, origin, destination, stops=1, msg="IndexError"):
    repository.log_fetch_error(
        check_date=check_date, origin=origin, destination=destination,
        departure_date="2026-10-09", stops=stops, via_airport=None,
        error_message=msg,
    )


def test_error_summary_groups_by_route_and_stops():
    _log_error("2026-08-08", "NRT", "SYD")
    _log_error("2026-08-09", "NRT", "SYD")
    _log_error("2026-08-09", "NRT", "SYD")     # 同じ日に2回（リトライ等）

    row = repository.fetch_error_summary("2026-08-01")[0]

    assert (row["origin"], row["destination"], row["stops"]) == ("NRT", "SYD", 1)
    assert row["first_seen"] == "2026-08-08"
    assert row["days_seen"] == 2        # 日数で数える
    assert row["occurrences"] == 3      # 回数は別に持つ


def test_a_persistent_failure_does_not_hide_a_new_one():
    """恒常的な失敗が1件あっても、新しい失敗を見落とさないこと。

    既知リストで握りつぶす方式を採らなかった理由がここ。記録は残したまま
    「初出がいつか」で分ければ、新規は必ず浮かび上がる。
    """
    _log_error("2026-08-08", "NRT", "SYD")     # 恒常的
    _log_error("2026-08-09", "NRT", "SYD")
    _log_error("2026-08-09", "HND", "CDG")     # 今日はじめて

    summary = repository.fetch_error_summary("2026-08-01")
    new = [e for e in summary if e["first_seen"] == "2026-08-09"]

    assert [(e["origin"], e["destination"]) for e in new] == [("HND", "CDG")]


def test_the_trend_takes_the_last_run_of_each_day():
    # 手で流し直した日は複数行入る。最終状態だけを見る。
    repository.insert_collection_log("2026-08-09", "partial", "manual", 24, 139, ["x"], 1000)
    repository.insert_collection_log("2026-08-09", "success", "manual", 24, 136, None, 1200)

    trend = repository.collection_trend("2026-08-01")

    assert len(trend) == 1
    assert trend[0]["status"] == "success"
    assert trend[0]["records_saved"] == 136


def test_the_trend_counts_errors_per_day():
    repository.insert_collection_log("2026-08-09", "partial", "scheduler", 60, 346, ["x"], 1000)
    _log_error("2026-08-09", "NRT", "SYD")
    _log_error("2026-08-09", "HND", "CDG")

    assert repository.collection_trend("2026-08-01")[0]["errors"] == 2
