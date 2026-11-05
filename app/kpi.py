"""日次 KPI の記録。

**目的は運用監視ではなく、拡張判断の材料を残すこと。** 「いま壊れているか」は
`status` / `health` で見る。ここが答えるのは「利用が伸びているか」「いまの上限で
あと何日もつか」「学習に使えるデータがどれだけ貯まったか」で、いずれも
**1日単位の点ではなく時系列にしないと読めない**。だから毎日1行を追記する。

CSV の列は英語で固定する。表計算や pandas に直接食わせる前提で、
日本語ヘッダだと環境によって文字化けや列名の扱いが面倒になるため。

**列は末尾にしか足さない。** 途中に挿入すると、過去に書いた行と列がずれる。
削除もしない（意味を失った列は 0 を書き続けるほうが、履歴が壊れるより良い）。
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.db import repository
from app import clock

logger = logging.getLogger("app.kpi")

# 列の順序＝CSV の列順。先頭は date で固定。
COLUMNS = [
    "date",
    # --- 需要（利用状況）---
    "watches_total",
    "watches_active",
    "watches_created_last_24h",
    "distinct_creators",
    # --- 定着（作られただけか、使われているか）---
    # **これが無いと、一度も開き直されていなくても KPI は好調に見える。**
    # 登録の勢いは需要を映すが、価値が届いたかは映さない。
    "watches_accessed_last_24h",
    "watches_accessed_last_7d",
    "watches_never_reopened",
    # 上の需要・定着の列は purpose='user' だけを数えている。**外した数を隠さない** —
    # これが無いと、CSV だけ見た人が watches_total を全モニター数と誤解する。
    "watches_research",
    # --- 容量（拡張の判断材料）---
    "routes_active",
    "routes_capacity",
    "routes_utilization_pct",
    # --- 当日の収集 ---
    "collection_status",
    "routes_checked",
    "records_collected",
    "collection_duration_seconds",
    "fetch_errors",
    # --- 蓄積したデータ（学習データとしての価値）---
    "snapshots_real",
    "snapshots_simulated",
    "observed_days",
    "series_total",
    "series_completed",
    "distinct_markets",
    "distinct_departure_dates",
    "distinct_airlines",
    "predictions_logged",
    "db_size_bytes",
    # --- 通知（課題4。2026-08-22 追加）---
    # **稼働している機能が KPI に出ていなかった。** 購読数も送信数も、
    # 本番の DB を直接見ないと分からない状態だった。
    "push_subscriptions",        # 購読している端末の数
    "push_watches",              # 購読者がいるモニターの数
    "push_sent_last_24h",        # その日に送った通知の数
    # **解除された数がいちばん要る。** 許可したのに切られた件数は、
    # 通知が歓迎されているかを測る唯一の手がかり。
    "push_gone_last_24h",
    # --- 予測の精度（課題9。2026-08-24 追加）---
    # **曲線を触るたびに良くなったか悪くなったかを言えるようにする。**
    # これが無いまま引き直すと、課題2で一度やった失敗（未検証のモデルを
    # 判定に使う）に戻る。
    "predictions_scored_total",   # 累計の採点済み件数
    "prediction_samples_7d",      # 直近7日で採点できた件数（下の3列の母数）
    "prediction_abs_error_pct",   # |予測−実測|/実測 の中央値
    # **偏りを別に持つ。** 絶対誤差だけだと「系統的に高い」ことに
    # 気づけない（初回測定で +15.3% だった）。
    "prediction_bias_pct",
    "prediction_band_hit_pct",    # 実測が信頼帯に入った割合
    # 裾（2026-09-10 追加）。**中央値だけでは裾が見えない。**
    # 半分が当たっていることは、残り半分がどれだけ外れたかを何も言わない。
    # 「9割はこのレート以内」を並べて出す。
    "prediction_abs_error_p90_pct",
    "prediction_bias_p90_pct",
    # **ここからが本命。** 上の全体値は、出発が遠い予測（曲線が平坦で
    # 当てるのが容易）に薄められる。2026-08-24 の実測では全体 0.5% に対し
    # 出発14日以内は 20.0%。**人が実際に買うのは直前**なので、そこを
    # 別に出さないと「精度は良い」と誤読する。
    "prediction_samples_near_7d",
    "prediction_abs_error_pct_near",
    "prediction_bias_pct_near",
    "prediction_band_hit_pct_near",
    "prediction_abs_error_p90_pct_near",
    "prediction_bias_p90_pct_near",
]


def _push_columns(today: date) -> dict:
    """通知まわり。**失敗しても KPI 全体を落とさない。**

    通知は後から足した機能で、KPI の本体（需要・容量・蓄積）とは独立して
    いる。ここで落ちて他の列まで記録されないほうが損失が大きい。
    """
    try:
        push = repository.push_kpi(today.isoformat())
    except Exception:
        logger.exception("push kpi failed")
        return {"push_subscriptions": "", "push_watches": "",
                "push_sent_last_24h": "", "push_gone_last_24h": ""}
    return {
        "push_subscriptions": push["subscriptions"],
        "push_watches": push["watches_with_push"],
        "push_sent_last_24h": push["sent"],
        "push_gone_last_24h": push["gone"],
    }


def _accuracy_columns(today: date) -> dict:
    """予測の精度。**失敗しても KPI 全体を落とさない**（push と同じ扱い）。

    採点はここではしない。日次ジョブの別の段が済ませている前提で、
    結果を読むだけ。KPI が採点まで持つと、KPI を手で流し直すたびに
    採点が走ることになる。
    """
    empty = {"predictions_scored_total": "", "prediction_samples_7d": "",
             "prediction_abs_error_pct": "", "prediction_bias_pct": "",
             "prediction_band_hit_pct": "",
             "prediction_abs_error_p90_pct": "",
             "prediction_bias_p90_pct": "",
             "prediction_samples_near_7d": "",
             "prediction_abs_error_pct_near": "",
             "prediction_bias_pct_near": "",
             "prediction_band_hit_pct_near": "",
             "prediction_abs_error_p90_pct_near": "",
             "prediction_bias_p90_pct_near": ""}
    try:
        from app import accuracy
        result = accuracy.measure(today)
        overall = result["overall"]
        near = result["near_departure"]
        return {
            "predictions_scored_total": repository.count_scored_predictions(),
            "prediction_samples_7d": overall["n"],
            # 採点できた件数が0の日は空欄。0 を書くと「誤差0%」に見える。
            "prediction_abs_error_pct": overall["abs_error_pct"]
                                        if overall["n"] else "",
            "prediction_bias_pct": overall["bias_pct"] if overall["n"] else "",
            "prediction_band_hit_pct": overall["band_hit_pct"]
                                       if overall["n"] else "",
            "prediction_abs_error_p90_pct": overall["abs_error_p90_pct"]
                                            if overall["n"] else "",
            "prediction_bias_p90_pct": overall["bias_p90_pct"]
                                       if overall["n"] else "",
            "prediction_samples_near_7d": near["n"],
            "prediction_abs_error_pct_near": near["abs_error_pct"]
                                             if near["n"] else "",
            "prediction_bias_pct_near": near["bias_pct"] if near["n"] else "",
            "prediction_band_hit_pct_near": near["band_hit_pct"]
                                            if near["n"] else "",
            "prediction_abs_error_p90_pct_near": near["abs_error_p90_pct"]
                                                 if near["n"] else "",
            "prediction_bias_p90_pct_near": near["bias_p90_pct"]
                                            if near["n"] else "",
        }
    except Exception:
        logger.exception("accuracy kpi failed")
        return empty


def collect_kpi(today: Optional[date] = None) -> dict:
    """その日の KPI を1行ぶん組み立てる。"""
    settings = get_settings()
    today = today or clock.today()
    since_24h = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    since_7d = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")

    row = repository.kpi_snapshot(today.isoformat(), since_24h, since_7d)

    capacity = settings.max_active_routes
    active = row["routes_active"]

    return {
        "date": today.isoformat(),
        "watches_total": row["watches_total"],
        "watches_active": row["watches_active"],
        "watches_created_last_24h": row["watches_created_last_24h"],
        "watches_accessed_last_24h": row["watches_accessed_last_24h"],
        "watches_accessed_last_7d": row["watches_accessed_last_7d"],
        "watches_never_reopened": row["watches_never_reopened"],
        "watches_research": row["watches_research"],
        "distinct_creators": row["distinct_creators"],
        "routes_active": active,
        "routes_capacity": capacity,
        "routes_utilization_pct": round(100.0 * active / capacity, 2) if capacity else 0.0,
        # 収集が一度も走っていない日は空文字。0 や 'success' を書くと
        # 「走って0件だった」と区別がつかなくなる。
        "collection_status": row["collection_status"] or "",
        "routes_checked": row["routes_checked"],
        "records_collected": row["records_collected"],
        "collection_duration_seconds": round(row["collection_duration_ms"] / 1000.0, 1),
        "fetch_errors": row["fetch_errors"],
        "snapshots_real": row["snapshots_real"],
        "snapshots_simulated": row["snapshots_simulated"],
        "observed_days": row["observed_days"],
        "series_total": row["series_total"],
        "series_completed": row["series_completed"],
        "distinct_markets": row["distinct_markets"],
        "distinct_departure_dates": row["distinct_departure_dates"],
        "distinct_airlines": row["distinct_airlines"],
        "predictions_logged": row["predictions_logged"],
        **_push_columns(today),
        **_accuracy_columns(today),
        "db_size_bytes": _db_size_bytes(settings.db_path),
    }


def _db_size_bytes(db_path: Path) -> int:
    """DB の実サイズ。WAL 分も足す（WAL は数十 MB になりうる）。"""
    total = 0
    for suffix in ("", "-wal"):
        f = Path(str(db_path) + suffix)
        if f.exists():
            total += f.stat().st_size
    return total


def append_row(row: dict, csv_path: Optional[Path] = None) -> Path:
    """CSV に1行追記する。同じ日付の行が既にあれば置き換える。

    置き換えるのは、日次ジョブの再実行や手動実行で同じ日が二重に載ると、
    行を数えるだけの集計（例: 稼働日数）が狂うため。
    """
    settings = get_settings()
    csv_path = csv_path or settings.kpi_csv_path
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    existing = _read_rows(csv_path)
    existing = [r for r in existing if r.get("date") != row["date"]]
    existing.append({c: row.get(c, "") for c in COLUMNS})
    existing.sort(key=lambda r: r.get("date", ""))

    # 一時ファイルへ書いてから差し替える。追記中に落ちると
    # 行が途中で切れた CSV が残り、翌日の読み込みごと壊れる。
    tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
    tmp.write_text(_to_csv(existing), encoding="utf-8")
    tmp.replace(csv_path)
    return csv_path


def _read_rows(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        return []
    with csv_path.open(newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: r.get(c, "") for c in COLUMNS})
    return buf.getvalue()


def upload_to_s3(csv_path: Optional[Path] = None) -> Optional[str]:
    """CSV を S3 へ写す。設定が無ければ何もしない。

    返り値は s3://... の URI、送らなかった場合は None。
    **例外は投げない。** 日次ジョブから呼ばれるので、S3 が落ちていることが
    価格収集の失敗になってはいけない。
    """
    settings = get_settings()
    if not settings.kpi_s3_bucket:
        return None

    csv_path = csv_path or settings.kpi_csv_path
    if not csv_path.exists():
        logger.warning("kpi csv not found, skipping upload: %s", csv_path)
        return None

    try:
        import boto3

        client = boto3.client("s3", region_name=settings.kpi_s3_region)
        client.put_object(
            Bucket=settings.kpi_s3_bucket,
            Key=settings.kpi_s3_key,
            Body=csv_path.read_bytes(),
            ContentType="text/csv",
        )
    except Exception:
        logger.exception("kpi upload to s3 failed")
        return None

    uri = f"s3://{settings.kpi_s3_bucket}/{settings.kpi_s3_key}"
    logger.info("kpi uploaded: %s", uri)
    return uri


def record_daily(today: Optional[date] = None) -> dict:
    """KPI を集めて CSV に書き、可能なら S3 へ写す。日次ジョブ用。"""
    row = collect_kpi(today)
    path = append_row(row)
    return {"row": row, "csv_path": str(path), "s3_uri": upload_to_s3(path)}
