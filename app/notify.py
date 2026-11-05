"""通知の送信（課題4・段階B）。

送るのは2つ。

    ① 観測開始以来の最安を 1% 以上更新したとき
    ② 値上がりが始まる日に届いたとき（1モニターにつき生涯1回）

**同じ日に両方該当したら②を優先する。** ②は時期が決まっていて生涯1回
しかない。①は翌日以降にも機会がある。

## なぜ 1% で切るか

日次の値動きは中央値 0.13%（ルート最安）で、最安更新の **54% は 1% 未満**。
薄い更新で通知を1回使うと、本当に効く通知の価値まで下がる。実測214系列での
試算では、1%で切ると週 0.40 回、3%まで締めても 0.31 回にしかならない
（docs/notification-design-2026-08-20.md）。

## 基準になる値

**その日より前の全観測の最小値**と比べる。`notified_low_usd` は「最後に
知らせた額」で、日次ジョブが二度走っても再送しないための記録。

## 送らないもの

- 実測が無いモニター（推定では送らない）
- 出発日を過ぎたモニター
- 購読者がいないモニター（そもそも回さない）
"""
import json
import logging
from datetime import date
from typing import Callable, Optional

from app import clock, fx
from app.config import Settings, get_settings
from app.db import repository
from app.predict.curve import route_params
from app.predict.engine import price_rises_after, recommend

logger = logging.getLogger(__name__)

# 最安の更新幅がこれ未満なら送らない。
LOW_MARGIN = 0.01

# ②を送ってよいのは、値上がり開始日から何日後までか。
#
# **日次ジョブが飛んだぶんを拾うためであって、遡って送るためではない。**
# 開始日を何週間も過ぎたモニターに「ここから値上がりします」を送ると、
# 事実として誤り（もう上がっている）。通知を導入する前に開始日を過ぎた
# モニターが、購読が付いた瞬間に一斉に鳴る、という事故もこれで防ぐ。
RISE_GRACE_DAYS = 7

# 1件の送信を何秒で諦めるか。
#
# **pywebpush は既定でタイムアウトを掛けない。** `webpush()` の既定引数は
# `timeout=None` で、それをそのまま `send()` へ渡す。`send()` 側は
# `kwargs.pop("timeout", 10000)` だが、キーが存在して値が None なので
# 既定の 10000 ではなく **None が採用され**、`requests.post(timeout=None)`
# となって**無期限に待つ**。
#
# 応答を返さない宛先が1件あると、そこで日次ジョブが止まる。段の並びは
# `… → notifications → kpi → dashboard` なので、**KPI 記録とダッシュボード
# 更新まで道連れになる。** 例外ではなく固まるので try/except では捕まらない。
#
# 購読の endpoint は外から登録できる（`POST /api/push/subscribe` は認証なし）
# ので、これは偶発の障害ではなく**仕掛けられる**。
SEND_TIMEOUT_SECONDS = 10

# 通知の文面。**断定しない。** 「これまでで一番安い」は観測の事実だが、
# 「買うべき」とは言わない — 谷の深さは −4.2% で、待てばもっと下がるかは
# 我々には言えない。
_TEXT = {
    "ja": {
        "title": "{origin}→{destination} が最安値を更新",
        "body": "{price}（これまでで一番安い値です）。出発 {departure}。",
        "body_rt": "{price}（往復・これまでで一番安い値です）。出発 {departure}、復路 {ret}。",
        "rise_title": "{origin}→{destination} はここから値上がりします",
        # **「いま買え」とは言わない。** 言えるのは「遅らせると高い」まで。
        "rise_body": "出発 {departure} まで {days} 日。ここから先は出発日に"
                     "近づくほど上がり、出発30日前を過ぎると平均{pct}%"
                     "高くなります。",
    },
    "en": {
        "title": "New low: {origin} to {destination}",
        "body": "{price} — the lowest we have seen. Departing {departure}.",
        "body_rt": "{price} round trip — the lowest we have seen. "
                   "Departing {departure}, returning {ret}.",
        "rise_title": "Fares start climbing: {origin} to {destination}",
        "rise_body": "{days} days until departure on {departure}. From here "
                     "fares rise as departure approaches, averaging {pct}% "
                     "higher once you are inside 30 days.",
    },
}


def _money(price_usd: float, lang: str) -> str:
    """日本語なら円も添える。**取れなければ USD のまま。**"""
    usd = f"US${price_usd:,.0f}"
    if lang != "ja":
        return usd
    rate = (fx.current() or {}).get("rate")
    return f"約{price_usd * rate:,.0f}円（{usd}）" if rate else usd


def build_low_payload(watch: repository.Watch, price_usd: float,
                      lang: str = "en") -> dict:
    """Service Worker がそのまま表示できる形にする。"""
    text = _TEXT.get(lang, _TEXT["en"])
    body_key = "body_rt" if watch.return_date else "body"
    return {
        "title": text["title"].format(origin=watch.origin,
                                      destination=watch.destination),
        "body": text[body_key].format(
            price=_money(price_usd, lang),
            departure=watch.departure_date,
            ret=watch.return_date),
        # 同じモニターの通知は置き換える。溜めても読まれない。
        "tag": f"low-{watch.token}",
        "url": f"/w/{watch.token}",
    }


def build_rise_payload(watch: repository.Watch, days_to_departure: int,
                       penalty_pct: int, lang: str = "en") -> dict:
    text = _TEXT.get(lang, _TEXT["en"])
    return {
        "title": text["rise_title"].format(origin=watch.origin,
                                           destination=watch.destination),
        "body": text["rise_body"].format(departure=watch.departure_date,
                                         days=days_to_departure,
                                         pct=penalty_pct),
        "tag": f"rise-{watch.token}",
        "url": f"/w/{watch.token}",
    }


def _send(subscription: repository.PushSubscription, payload: dict,
          settings: Settings) -> str:
    """1件送る。戻り値は 'sent' | 'gone' | 'failed'。

    **'gone' は購読が死んでいる**（利用者がブラウザ側で切った、または
    期限切れ）。こちらから死活監視はできないので、送ってはじめて分かる。
    """
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh,
                         "auth": subscription.auth},
            },
            data=json.dumps(payload),
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject},
            ttl=86400,      # 1日届かなければ捨てる。翌日には別の値になる
            # **明示しないと無期限に待つ。** 上の SEND_TIMEOUT_SECONDS を見よ。
            timeout=SEND_TIMEOUT_SECONDS,
        )
        return "sent"
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            return "gone"
        logger.warning("push failed (%s): %s", status, exc)
        return "failed"
    except Exception as exc:
        # **プッシュサービスが落ちていると購読の数だけ出る。** 全トレースを
        # 積むとログが埋まって他の障害が見えなくなるので、通信の失敗は
        # 1行に落とす。想定外のものだけトレースを残す。
        import requests
        if isinstance(exc, requests.exceptions.RequestException):
            logger.warning("push failed (通信): %s", type(exc).__name__)
        else:
            logger.exception("push failed")
        return "failed"


def _current_best(watch: repository.Watch, today: date) -> Optional[float]:
    """いま買える最安。**画面に出ているものと同じ値にする。**

    利用者が見る額と通知の額が違うと信用を失うので、画面と同じ経路
    （recommend）を通す。航空会社の絞り込みと所要時間の上限も効く。
    """
    result = recommend(
        watch.origin, watch.destination, watch.departure_date, today,
        watch.airline_filter or None,
        max_total_minutes=watch.max_total_minutes,
        return_date=watch.return_date,
    )
    if not result:
        return None
    best = result.get("current_best")
    # **推定では送らない。** basis が observed 系のときだけ。
    if not best or result.get("basis") not in ("observed", "round_trip_observed"):
        return None
    return float(best["price_usd"])


def _baseline(watch: repository.Watch, today: date) -> Optional[float]:
    """その日より前の全観測の最小値。比べる相手。

    `notified_low_usd` があればそれも下限に入れる。日次ジョブが二度
    走ったときに同じ通知を繰り返さないため。
    """
    rows = repository.history_for(watch.origin, watch.destination,
                                  watch.departure_date,
                                  return_date=watch.return_date)
    past = [r["price_usd"] for r in rows
            if r["check_date"] < today.isoformat()]
    candidates = [p for p in (min(past) if past else None,
                              watch.notified_low_usd) if p is not None]
    return min(candidates) if candidates else None


def _rise_due(watch: repository.Watch, today: date) -> Optional[dict]:
    """②を送るべきか。送るなら price_rises_after の結果を返す。

    **判定の日付は画面と同じものを使う。** 画面は「◯月◯日から値上がりが
    始まります」と出しており、別の日に通知が来ると説明できない。

    戻り値は次の3通り。

        dict    … 送る
        "skip"  … 送らないと決めた。呼び出し側が記録して二度と評価しない
        None    … 今日は該当しない（明日また見る）

    送らない場合:

    - **往復。** 遅延の罰(+16.7%)は片道212系列から出した値で、往復では
      未測定（docs/round-trip-findings-2026-08-19.md §5）
    - 既に送った（生涯1回）
    - **モニターを作った時点で既にその日を過ぎていた。** 「これから
      上がります」という知らせであって「もう上がっています」ではない
    - **開始日から RISE_GRACE_DAYS を過ぎた。** 同じ理由で手遅れ
    """
    if watch.return_date:
        return None
    if watch.notified_rise_at:
        return None

    # 期限は方向で違う（curve.route_params）。画面と同じ日に知らせる。
    rises = price_rises_after(watch.departure_date, today,
                              route_params(watch.origin, watch.destination))
    if rises is None:
        return None                     # 出発済み
    if rises["days"] > 0:
        return None                     # まだ来ていない

    created = watch.created_at[:10]
    if rises["date"] < created:
        return "skip"                   # 作った時点で既に過ぎていた
    if rises["days"] < -RISE_GRACE_DAYS:
        # **手遅れ。** 送らないと決めたので、記録だけして二度と評価しない。
        return "skip"
    return rises


def _deliver(watch: repository.Watch, payload_for, today: date,
             settings: Settings, sender: Callable, summary: dict) -> None:
    """そのモニターの購読者すべてに送る。**1件の失敗で残りを止めない。**"""
    for subscription in repository.push_subscriptions_for_watch(watch.id):
        result = sender(subscription, payload_for(subscription.lang), settings)
        summary[result] = summary.get(result, 0) + 1
        if result == "gone":
            repository.delete_push_subscription(subscription.endpoint)
        elif result == "sent":
            repository.mark_subscription_sent(subscription.id, today.isoformat())


def run(today: Optional[date] = None, settings: Optional[Settings] = None,
        sender: Optional[Callable] = None) -> dict:
    """通知を送る。**例外を投げない。** 日次ジョブの一段として呼ばれる。

    **1モニターにつき1日1通まで。** 両方該当したら②を優先する — ②は
    時期が決まっていて生涯1回しかないが、①は翌日以降にも機会がある。

    sender を差し替えられるのはテストのため。実際に通信すると
    ネットワークに依存したテストになる。
    """
    settings = settings or get_settings()
    today = today or clock.today()
    sender = sender or _send
    summary = {"checked": 0, "low": 0, "rise": 0,
               "sent": 0, "gone": 0, "failed": 0}

    if not settings.push_enabled:
        summary["skipped"] = "鍵が未設定"
        return summary

    for watch in repository.watches_with_push_subscribers():
        summary["checked"] += 1
        if watch.departure_date < today.isoformat():
            continue        # 出発済み

        # --- ② 値上がりの警告。生涯1回なので先に見る ---
        rises = _rise_due(watch, today)
        if rises == "skip":
            # 送らないと決めたものを、毎日評価し直さない。
            repository.mark_notified_rise(watch.id, today.isoformat())
            rises = None
        if rises is not None:
            summary["rise"] += 1
            # 先に記録する。送信の途中で落ちても二度送らない。
            repository.mark_notified_rise(watch.id, today.isoformat())
            days = (date.fromisoformat(watch.departure_date) - today).days
            _deliver(watch,
                     lambda lang: build_rise_payload(
                         watch, days, rises["penalty_pct"], lang),
                     today, settings, sender, summary)
            continue        # この日は①を送らない

        # --- ① 最安の更新 ---
        best = _current_best(watch, today)
        if best is None:
            continue
        baseline = _baseline(watch, today)
        # 比べる相手が無い＝初日。**初日に「最安更新」は言えない。**
        if baseline is None or best >= baseline * (1 - LOW_MARGIN):
            continue

        summary["low"] += 1
        repository.mark_notified_low(watch.id, best)
        _deliver(watch, lambda lang: build_low_payload(watch, best, lang),
                 today, settings, sender, summary)

    # **走ったことを必ず残す。** 0件の日も記録する — 記録が無い日と
    # 「走ったが送るものが無かった日」を区別できないと、止まっていたことに
    # 気づけない。
    repository.insert_notification_log(today.isoformat(), summary)
    return summary


def notify_new_lows(today=None, settings=None, sender=None) -> dict:
    """旧名。段階Bで作った入口を残してある。"""
    return run(today, settings, sender)
