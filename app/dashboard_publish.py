"""ダッシュボードを S3 に書き出す（日次ジョブの最後の段）。

**なぜ S3 経由なのか。** 毎朝ダッシュボードを更新するのはクラウド側の
ルーティンだが、その実行環境は外向き通信が制限されており、
flights.suosolutions.com には届かない（2026-09-01 に実測: プロキシが
CONNECT を拒否）。一方 S3 には届く。日次ジョブが既に KPI を上げている
経路なので、同じところにダッシュボードも置く。
"""
import json
import logging
from typing import Optional

from app import clock
from app.config import Settings, get_settings
from app.dashboard_build import render_dashboard
from app.db import repository

logger = logging.getLogger(__name__)


def build_payload(settings: Optional[Settings] = None) -> dict:
    """取得口と同じ中身。**1か所で作る** — 2つの経路で別物になると困る。"""
    from app.api.routes import _ops_kpi_rows
    settings = settings or get_settings()
    from app import buy_timing
    return {
        "generated_at": clock.today().isoformat(),
        "kpi": _ops_kpi_rows(settings.kpi_csv_path),
        "drops": repository.largest_drops(limit=10, min_points=5),
        "meta": repository.ops_meta(),
        # 買い時（底値の時期）の答え合わせ。**アプリの目的に沿った精度。**
        "buy_timing": buy_timing.evaluate(),
    }


def publish(settings: Optional[Settings] = None) -> dict:
    """JSON と日英の HTML を S3 に置く。返り値は置いた URL。"""
    settings = settings or get_settings()
    if not settings.dashboard_s3_bucket or not settings.dashboard_s3_prefix:
        return {"skipped": "書き出し先が未設定"}

    ops = build_payload(settings)
    if not ops["kpi"]:
        # **空の殻を置かない。** 推移を見せる画面なので、1日も無いまま
        # 上書きすると、前日まで見えていたものまで壊す。
        return {"skipped": "KPI がまだ1日ぶんも無い"}

    import boto3
    client = boto3.client("s3", region_name=settings.dashboard_s3_region)
    base = f"d/{settings.dashboard_s3_prefix}"
    written = {}

    def put(name: str, body: str, content_type: str) -> None:
        client.put_object(Bucket=settings.dashboard_s3_bucket,
                          Key=f"{base}/{name}",
                          Body=body.encode("utf-8"),
                          ContentType=content_type,
                          CacheControl="no-cache")
        written[name] = (f"https://{settings.dashboard_s3_bucket}"
                         f".s3.{settings.dashboard_s3_region}.amazonaws.com/{base}/{name}")

    put("dashboard.json", json.dumps(ops, ensure_ascii=False), "application/json")
    for lang in ("ja", "en"):
        put(f"dashboard.{lang}.html", render_dashboard(ops, lang),
            "text/html; charset=utf-8")

    logger.info("dashboard published: %s", list(written))
    return {"written": written, "kpi_days": len(ops["kpi"]),
            "drops": len(ops["drops"])}
