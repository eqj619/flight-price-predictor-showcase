"""日次ジョブのスケジューラ（APScheduler）。

PI-27-flightTrend の scheduler.js から `isRunning` ガードを引き継ぐ。収集は
500ルートで90分近くかかりうるので、**前回がまだ走っているなら今回は黙って
飛ばす**。二重に走ると Google Flights へのリクエストが倍になり、ポライトネス
間隔を守っている意味が無くなる。
"""
import logging
import threading
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings, get_settings
from app.jobs.daily import run_daily

logger = logging.getLogger("app.jobs")

_JOB_ID = "daily"


class DailyScheduler:
    """日1回 run_daily を呼ぶ。多重起動しない。"""

    def __init__(self, settings: Optional[Settings] = None,
                 job: Callable[[], dict] = run_daily):
        self._settings = settings or get_settings()
        self._job = job
        self._scheduler: Optional[BackgroundScheduler] = None
        # ガードはロックで守る。APScheduler はワーカースレッドで実行するので、
        # 素のフラグだと起動が重なったときに両方が通り抜けうる。
        self._lock = threading.Lock()
        self._running = False
        self.last_started_at: Optional[str] = None
        self.last_result: Optional[dict] = None
        self.last_skipped_at: Optional[str] = None

    # ------------------------------------------------------------------

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self._settings.timezone)

    @property
    def is_running(self) -> bool:
        return self._running

    def status(self) -> dict:
        job = self._scheduler.get_job(_JOB_ID) if self._scheduler else None
        return {
            "enabled": self._settings.scheduler_enabled,
            "running": self._running,
            "timezone": self._settings.timezone,
            "schedule": f"{self._settings.daily_run_hour:02d}:"
                        f"{self._settings.daily_run_minute:02d}",
            "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
            "last_started_at": self.last_started_at,
            "last_skipped_at": self.last_skipped_at,
            "last_result": self.last_result,
        }

    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self._settings.scheduler_enabled:
            logger.info("scheduler is disabled by configuration")
            return
        if self._scheduler is not None:
            return

        self._scheduler = BackgroundScheduler(timezone=self.timezone)
        self._scheduler.add_job(
            self.run_once,
            CronTrigger(hour=self._settings.daily_run_hour,
                        minute=self._settings.daily_run_minute,
                        timezone=self.timezone),
            id=_JOB_ID,
            # 停止していた間に実行時刻を過ぎていたら、この猶予内なら追いつく。
            misfire_grace_time=self._settings.job_misfire_grace_seconds,
            # APScheduler 側でも重複実行を止める（ガードとの二重防御）。
            max_instances=1,
            coalesce=True,      # 溜まった実行を1回にまとめる
        )
        self._scheduler.start()
        logger.info("scheduler started: daily at %02d:%02d %s",
                    self._settings.daily_run_hour,
                    self._settings.daily_run_minute,
                    self._settings.timezone)

    def shutdown(self) -> None:
        if self._scheduler is None:
            return
        # 走っている収集を待たずに落とす。途中まで書いた snapshots は
        # upsert なので、翌日の実行で埋め直される。
        self._scheduler.shutdown(wait=False)
        self._scheduler = None

    # ------------------------------------------------------------------

    def run_once(self) -> Optional[dict]:
        """ジョブ本体。実行中なら何もせず None を返す。"""
        with self._lock:
            if self._running:
                self.last_skipped_at = datetime.now(self.timezone).isoformat(
                    timespec="seconds")
                logger.warning("previous run is still in progress; skipping")
                return None
            self._running = True
            self.last_started_at = datetime.now(self.timezone).isoformat(
                timespec="seconds")

        try:
            self.last_result = self._job()
            return self.last_result
        except Exception:
            # run_daily は投げない作りだが、ここで漏らすとスケジューラの
            # ジョブごと停止しうるので最後の壁を置く。
            logger.exception("daily job raised unexpectedly")
            return None
        finally:
            with self._lock:
                self._running = False


# FastAPI の lifespan から使う共有インスタンス。
scheduler = DailyScheduler()
