"""日次ジョブ。ライフサイクル整理 -> 価格収集 -> 予測の記録。

**この関数は例外を投げない。** 既存 flight-price-monitor の run_daily.py の
方針をそのまま引き継ぐ。無人で回る前提なので、1回の失敗を人が見に来なくても
翌日には自力で戻ってくる必要がある。段ごとに try/except を分けてあるのは、
収集が失敗しても失効整理は済ませておきたいため。

順序に意味がある。**整理を先にやってから収集する。** 逆にすると、今日
失効するモニターのために今日の分をもう一度取りに行ってしまう。
"""
import logging
import time
from datetime import date
from typing import Optional

from app import clock
from app.collect.provider import PriceProvider
from app.config import get_settings
from app.db import repository

logger = logging.getLogger("app.jobs")


def check_route_capacity() -> dict:
    """ルート枠の残りを見て、逼迫していれば警告する。

    **上限に達してから気づくと、利用が伸びている最中に受付を止めることに
    なる。** 実際 2026-08-14 に利用者由来の増加が 47本/日 に達し、残り6日
    まで詰まっていたが、気づいたのは手で KPI を眺めたからだった。

    止めることはしない — 判断は人がする。ログに出すだけ。
    """
    settings = get_settings()
    active = repository.count_active_routes()
    cap = settings.max_active_routes
    share = active / cap if cap else 1.0
    state = {"active": active, "capacity": cap, "share": round(share, 3)}

    if share >= 1.0:
        logger.error("ルート枠が上限に達した: %d/%d。新規登録は拒否される", active, cap)
        state["level"] = "full"
    elif share >= settings.route_capacity_warn_fraction:
        logger.error("ルート枠が逼迫: %d/%d (%.0f%%)。上限の見直しが要る",
                     active, cap, share * 100)
        state["level"] = "warn"
    else:
        state["level"] = "ok"
    return state


def disable_failing_routes() -> dict:
    """収集しても何も取れないルートを止める。

    **全面障害で全部を止めないための歯止めを必ず通す。** fast_flights は
    非公式スクレイパで、Google 側の変更で全ルートが同時に壊れうる。そのとき
    自動停止が働くと、監視対象を自ら全滅させたうえ「静かに収集ゼロ」に
    なるので気づくのも遅れる。候補が稼働ルートの一定割合を超えたら、
    1本も止めずに警告だけ出す。
    """
    settings = get_settings()
    dates = repository.recent_collection_dates(settings.auto_disable_after_failed_days)
    if len(dates) < settings.auto_disable_after_failed_days:
        # 収集の実績がまだ足りない。判断材料が無い状態で止めない。
        return {"disabled": 0, "candidates": 0, "skipped": "収集日数が不足"}

    # **2つの根拠を別の窓で数える。**
    #
    #   エラー   3日  こちらの不調。証拠として強い
    #   便0件    5日  上流が不調な日にも出る。弱いので長く見る
    #
    # 短いほうの窓では fetch_failed だけを採り、no_flights は長いほうの窓で
    # 改めて判定する。混ぜると、0件が3日続いただけで「運航されていない」と
    # 断定してしまう（2026-09-01 に実在路線17本がその状態になった）。
    candidates = [c for c in repository.routes_failing_consistently(dates)
                  if c["kind"] == "fetch_failed"]

    empty_dates = repository.recent_collection_dates(
        settings.auto_disable_after_empty_days)
    if len(empty_dates) >= settings.auto_disable_after_empty_days:
        seen = {c["id"] for c in candidates}
        # **予約受付前の日の「0件」は証拠に数えない。** 航空会社は出発の
        # 約330日前まで予約を受け付けないので、それより先の出発日は検索しても
        # 便が出てこないのが正常（2026-10-11 に SAN-NRT 57本をこれで誤停止）。
        #
        # 判定に使う窓の**いちばん古い日**で測る。その日に予約受付前だったなら、
        # 窓の中に「受付前の空振り」が混ざっている。今日受付に入ったばかりの
        # ルートを、受付前の0件で止めないため。
        oldest = date.fromisoformat(min(empty_dates))
        for c in repository.routes_failing_consistently(empty_dates):
            if c["kind"] != "no_flights" or c["id"] in seen:
                continue
            ahead = (date.fromisoformat(c["departure_date"]) - oldest).days
            if ahead > settings.booking_horizon_days:
                continue
            candidates.append(c)

    active = repository.count_active_routes()
    if not candidates:
        return {"disabled": 0, "candidates": 0}

    # **割合と件数の両方を満たしたときだけ「全面障害」と見なす。**
    # 割合だけだと、稼働2本で1本が不良なら 50% になり、永久に止められない。
    share = len(candidates) / active if active else 1.0
    if (share > settings.auto_disable_max_fraction
            and len(candidates) >= settings.auto_disable_min_widespread):
        logger.error(
            "自動停止を見送り: 候補 %d / 稼働 %d (%.0f%%) が閾値 %.0f%% を超えた。"
            "個別の不良ではなく全面障害の可能性がある",
            len(candidates), active, share * 100,
            settings.auto_disable_max_fraction * 100)
        return {"disabled": 0, "candidates": len(candidates),
                "skipped": "全面障害の疑い"}

    # **理由を分けて残す。** 利用者に見せる文言が変わる。「取れなかった」は
    # こちらの不調、「便が無い」は区間そのものが運航されていない可能性。
    # **判定に使った窓で書く。** 便0件は5日、取得失敗は3日と窓が違う。
    # 2026-10-11 まで両方を3日の窓で書いており、実際は5日で判定した停止が
    # 「3回連続（10/08〜10/10）」と記録されていた。この文言は利用者にも出る。
    def _period(ds):
        return f"（{min(ds)}〜{max(ds)}）"
    reasons = {
        "no_flights": f"{len(empty_dates)}回連続で便が0件{_period(empty_dates)}",
        "fetch_failed": f"{len(dates)}回連続で取得できず{_period(dates)}",
    }
    disabled = 0
    for kind, reason in reasons.items():
        ids = [c["id"] for c in candidates if c.get("kind") == kind]
        disabled += repository.disable_routes(ids, reason, kind)
    for c in candidates:
        logger.warning("収集を停止(%s): %s->%s %s", c.get("kind"),
                       c["origin"], c["destination"], c["departure_date"])
    return {"disabled": disabled, "candidates": len(candidates),
            "no_flights": sum(1 for c in candidates
                              if c.get("kind") == "no_flights")}


def run_lifecycle(today: Optional[date] = None) -> dict:
    """失効したモニターと、誰も見ていないルートを片付ける。

    価格履歴（snapshots）は消さない。収集済みの価格は資産として残し、
    同じルートを誰かが再登録したとき即座に過去トレンドを見せられるようにする。
    """
    today = today or clock.today()
    today_iso = today.isoformat()

    # 順序: watch を消す -> 参照されなくなったルートを止める。
    # 逆にすると、消える直前の watch がルートを生かしたままになる。
    expired_watches = repository.delete_expired_watches(today_iso)
    orphan_routes = repository.deactivate_orphan_routes()
    departed_routes = repository.deactivate_departed_routes(today_iso)
    # 一覧は watch の後に片付ける。先に消すと、まだ生きている一覧の期限が
    # 「所属モニターの最長期限」で引き直される前に判定してしまう。
    expired_collections = repository.delete_expired_collections(today_iso)
    # **収集の前に判定する。** ここで止めれば当日のリクエストも節約できる。
    failing = disable_failing_routes()
    capacity = check_route_capacity()

    return {
        "expired_watches": expired_watches,
        "orphan_routes": orphan_routes,
        "departed_routes": departed_routes,
        "expired_collections": expired_collections,
        "failing_routes": failing,
        "route_capacity": capacity,
    }


def run_daily(provider: Optional[PriceProvider] = None,
              today: Optional[date] = None) -> dict:
    """1日分の処理をひととおり回す。例外は投げない。"""
    today = today or clock.today()
    started = time.monotonic()
    summary = {
        "date": today.isoformat(),
        "lifecycle": None,
        "collection": None,
        "predictions_logged": 0,
        "kpi": None,
        "notifications": None,
        "accuracy": None,
        "failed_steps": [],
    }

    # --- 1. ライフサイクル ---
    try:
        summary["lifecycle"] = run_lifecycle(today)
        logger.info("lifecycle: %s", summary["lifecycle"])
    except Exception:
        logger.exception("lifecycle step failed")
        summary["failed_steps"].append("lifecycle")

    # --- 2. 収集 ---
    try:
        from app.collect.collector import collect_once
        summary["collection"] = collect_once(provider, today)
        logger.info("collection: %s", summary["collection"])
    except Exception:
        # collect_once 自体が例外を投げない作りだが、provider の生成や
        # DB 接続で落ちる余地は残る。
        logger.exception("collection step failed")
        summary["failed_steps"].append("collection")

    # --- 3. 予測の記録（精度検証用）---
    try:
        summary["predictions_logged"] = _log_predictions(today)
    except Exception:
        logger.exception("prediction logging step failed")
        summary["failed_steps"].append("predictions")

    # --- 3b. 買い時の判定の記録（底値の当否を検証するため）---
    # 予測の段とは別に囲う。こちらが落ちても予測の記録は残す。
    try:
        summary["verdicts_logged"] = _log_verdicts(today)
    except Exception:
        logger.exception("verdict logging step failed")
        summary["failed_steps"].append("verdicts")

    # --- 4. 為替の更新 ---
    # 失敗しても収集や予測には影響しない（表示の換算にしか使わない）。
    try:
        from app import fx
        summary["fx"] = fx.refresh()
    except Exception:
        logger.exception("fx step failed")
        summary["failed_steps"].append("fx")

    # --- 4.5 予測の答え合わせ（課題9）---
    # **収集の後。** その日の実測が入ってから採点する。前に置くと、
    # 対象日の実測がまだ無い状態で「比べる相手なし」と判定してしまう。
    try:
        from app import accuracy
        summary["accuracy"] = accuracy.score_and_measure(today)
        logger.info("accuracy: %s", summary["accuracy"].get("overall"))
    except Exception:
        logger.exception("accuracy step failed")
        summary["failed_steps"].append("accuracy")

    # --- 5. 通知（課題4）---
    # **収集と予測の後。** 当日の価格を見て判定するので、前に置くと
    # 前日の値で送ることになる。為替の後にも置く（日本語の本文で円を出す）。
    #
    # 失敗しても他の段には影響しない。通知が飛ばないより、収集が止まる
    # ほうがずっと痛い。
    try:
        from app import notify
        summary["notifications"] = notify.run(today)
        logger.info("notifications: %s", summary["notifications"])
    except Exception:
        logger.exception("notification step failed")
        summary["failed_steps"].append("notifications")

    # --- 6. KPI の記録 ---
    # 収集の後に置く。前に置くと当日の収集結果が空のまま記録される。
    try:
        from app import kpi
        result = kpi.record_daily(today)
        summary["kpi"] = {"csv_path": result["csv_path"], "s3_uri": result["s3_uri"]}
        logger.info("kpi recorded: %s", summary["kpi"])
    except Exception:
        logger.exception("kpi step failed")
        summary["failed_steps"].append("kpi")

    # **KPI の後。** ダッシュボードは KPI の CSV を読むので、順序が逆だと
    # 前日ぶんで作ってしまう。
    try:
        from app import dashboard_publish
        summary["dashboard"] = dashboard_publish.publish()
        logger.info("dashboard: %s", summary["dashboard"])
    except Exception:
        logger.exception("dashboard step failed")
        summary["failed_steps"].append("dashboard")

    summary["duration_ms"] = int((time.monotonic() - started) * 1000)
    logger.info("daily job done in %d ms (failed steps: %s)",
                summary["duration_ms"], summary["failed_steps"] or "none")
    return summary


def _log_verdicts(today: date) -> int:
    """active な片道ルートについて、今日の買い時の判定を残す。

    **アプリの目的は「いま底値の時期か」を言い当てること。** それなのに、
    2026-10-12 まで今買う／待つ／様子見の判定を一度も記録しておらず、当否を
    検証できなかった。出発日を迎えたら、判定した日の価格が底値にどれだけ
    近かったかを測る（app/buy_timing.py）。

    **絞り込みなしで判定する**（航空会社・所要時間の上限をかけない）。答え合わせ
    の相手が「その日そのルートで最も安い運賃」なので、条件を揃える。
    往復は判定を出していないので対象外。1ルートの失敗で他を巻き込まない。
    """
    from app.ml import drop_model
    from app.predict.engine import recommend

    # 影の機械学習。**失敗しても判定の記録は続ける**（中で例外を握る）。
    ml_probs, ml_tag = drop_model.shadow_probabilities(today)

    logged = 0
    for route in repository.active_routes():
        if route.return_date:
            continue
        try:
            rec = recommend(route.origin, route.destination, route.departure_date,
                            today=today, airline_filter=None, max_total_minutes=None)
        except Exception:
            logger.exception("verdict failed for %s->%s %s",
                             route.origin, route.destination, route.departure_date)
            continue
        if not rec or not rec.get("verdict"):
            continue
        best = rec.get("current_best") or {}
        pmin = rec.get("predicted_min") or {}
        rise = rec.get("price_rises_after") or {}
        repository.upsert_verdict(
            logged_on=today.isoformat(),
            origin=route.origin, destination=route.destination,
            departure_date=route.departure_date,
            days_before_departure=(date.fromisoformat(route.departure_date) - today).days,
            verdict=rec["verdict"], basis=rec.get("basis"),
            observed_days=rec.get("observed_days"),
            current_price_usd=best.get("price_usd"),
            predicted_min_usd=pmin.get("price_usd"),
            predicted_min_upper_usd=pmin.get("upper_usd"),
            predicted_min_date=pmin.get("check_date"),
            rise_date=rise.get("date"),
            ml_drop_prob=ml_probs.get((route.origin, route.destination,
                                       route.departure_date)),
            ml_model=ml_tag,
        )
        logged += 1
    return logged


def _log_predictions(today: date) -> int:
    """active な各ルートについて、今日時点の予測を残す。

    1ルートの予測が失敗しても他を巻き込まない。
    """
    from app.predict.engine import log_predictions

    total = 0
    for route in repository.active_routes():
        try:
            total += log_predictions(route.origin, route.destination,
                                     route.departure_date, predicted_on=today)
        except Exception:
            logger.exception("prediction failed for %s->%s %s",
                             route.origin, route.destination, route.departure_date)
    return total
