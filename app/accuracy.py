"""予測の精度（課題9）。

**「当たっているか」を毎日測って残す。** 曲線を触るたびに、良くなったのか
悪くなったのかを言えるようにするため。これが無いまま曲線を引き直すと、
課題2で一度やった失敗（未検証のモデルを判定に使う）に戻る。

## 何を測るか

    絶対誤差の中央値   |予測 − 実測| / 実測 の中央値。当たり具合
    偏り（中央値）     (予測 − 実測) / 実測 の中央値。**高すぎ/安すぎの向き**
    帯に入った割合     実測が [lower, upper] に入った割合
    誤差 P90          9割の予測が収まっている誤差のレート。**裾の大きさ**
    偏り P90          9割の予測の偏りが収まっているレート（高く出す側の裾）

**中央値だけでは裾が見えない。** 半分が当たっていることは、残り半分が
どれだけ外れたかを何も言わない。買う人が困るのは裾のほうで、
「たいてい当たる」と言いながら1割が大きく外れているなら伝えるべき。

**偏りを別に持つのが要。** 絶対誤差だけ見ていると、系統的に高い（実際
2026-08-24 の初回測定で +15.3%）ことに気づけない。ばらつきと偏りは
直し方が違う。

## 出発までの日数で分ける — **まとめた数字は嘘になる**

2026-08-24 の実測（5,216件）:

    出発14日以内   n=414   絶対誤差 20.0%  偏り +18.7%  帯 49%
    15〜30日前     n=487   絶対誤差  2.6%  偏り  +1.7%  帯 73%
    31〜60日前     n=937   絶対誤差  0.9%  偏り  +0.0%  帯 81%
    121日以上前    n=1688  絶対誤差  0.2%  偏り  +0.0%  帯 88%

**全体では 0.5% になる。** 出発が遠い予測が大半で、そこでは曲線が平坦
＝「直近の値をそのまま伸ばす」だけなので当たって当然。**誤差は直前に
集中しており、そこは人が実際に買う時期。**

したがって KPI には**出発30日以内の数字を別に出す。** 全体だけ見ていると
「精度0.5%」と読めてしまい、肝心なところが20%外れていることに気づけない。

## 先読みでも分ける

誤差は先読みとともにも伸びる（1日先 9.4% → 7日先 35.9%）。
まとめて1つの数字にすると、地平線の構成が変わっただけで動く。
"""
from __future__ import annotations

import logging
import math
import statistics as st
from datetime import date, timedelta
from typing import Optional

from app import clock
from app.db import repository

logger = logging.getLogger(__name__)

# KPI に出す集計の対象期間。**日をまたいで安定させるために窓を取る。**
# 1日だけだと、その日たまたま採点できた件数の少なさで大きく振れる。
WINDOW_DAYS = 7

# 「直前」の境。ここより内側を別に集計する。
#
# 誤差がいちばん濃いのは出発14日以内だが、7日窓では件数が足りない日が
# 出る。30日にすると転移領域（15〜30日前は誤差2.6%）が混ざって薄まるが、
# **薄まっても全体（0.5%）よりはるかに実態を映す。** 内訳が要るときは
# by_days_before_departure を見る。
NEAR_DEPARTURE_DAYS = 30


def _p90(values: list[float]) -> float:
    """90 パーセンタイル。**順位で取る。補間しない。**

    `statistics.quantiles` は既定で線形補間するので、件数が少ない日に
    実在しない値を返す。ここで見たいのは「実際に起きた外れ方の大きさ」
    なので、**実在する観測値のどれか**であってほしい。

    最近傍順位法（ceil(0.9 * n) 番目、1始まり）を使う。n=1 ならその値。
    """
    ordered = sorted(values)
    index = math.ceil(0.9 * len(ordered)) - 1
    return ordered[max(index, 0)]


def _summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "abs_error_pct": None, "bias_pct": None,
                "band_hit_pct": None,
                "abs_error_p90_pct": None, "bias_p90_pct": None}
    errors = [(r["predicted_usd"] - r["actual_usd"]) / r["actual_usd"] * 100
              for r in rows]
    hits = [r["lower_usd"] <= r["actual_usd"] <= r["upper_usd"] for r in rows]
    return {
        "n": len(rows),
        "abs_error_pct": round(st.median(abs(e) for e in errors), 1),
        "bias_pct": round(st.median(errors), 1),
        "band_hit_pct": round(100.0 * sum(hits) / len(hits), 1),
        # **中央値は裾を隠す。** 半分が当たっていることは、残り半分が
        # どれだけ外れたかを何も言わない。買う人が困るのは裾のほうで、
        # 「たいてい当たる」と言いながら1割が大きく外れているなら、
        # それは伝えるべき情報になる。
        #
        #   誤差 P90 … 9割の予測がこのレート以内に収まっている
        #   偏り P90 … 9割の予測の偏りがこのレート以下（＝高く出す側の裾）
        #
        # 偏りは符号付きのまま取る。絶対値で取ると誤差 P90 と同じ数字に
        # なり、**向き**という偏りを別に持つ理由が消える。
        "abs_error_p90_pct": round(_p90([abs(e) for e in errors]), 1),
        "bias_p90_pct": round(_p90(errors), 1),
    }


def measure(today: Optional[date] = None, window_days: int = WINDOW_DAYS) -> dict:
    """直近の採点済み予測から精度を出す。

    全体と、先読み日数ごとの両方を返す。KPI に載せるのは全体だけだが、
    調べるときは内訳が要る。
    """
    today = today or clock.today()
    since = (today - timedelta(days=window_days)).isoformat()
    rows = repository.prediction_scores(since)

    by_lead: dict[int, list[dict]] = {}
    by_bucket: dict[str, list[dict]] = {}
    for row in rows:
        by_lead.setdefault(row["lead_days"], []).append(row)
        by_bucket.setdefault(_bucket(row["days_before_departure"]), []).append(row)

    near = [r for r in rows
            if r["days_before_departure"] <= NEAR_DEPARTURE_DAYS]

    return {
        "window_days": window_days,
        "overall": _summarize(rows),
        # **人が実際に買う時期。ここが本命の数字。**
        "near_departure": _summarize(near),
        "by_lead_days": {k: _summarize(v) for k, v in sorted(by_lead.items())},
        "by_days_before_departure": {k: _summarize(v)
                                     for k, v in sorted(by_bucket.items())},
    }


_BUCKETS = ((14, "0-14"), (30, "15-30"), (60, "31-60"), (120, "61-120"))


def _bucket(days_before: int) -> str:
    for limit, label in _BUCKETS:
        if days_before <= limit:
            return label
    return "121+"


def score_and_measure(today: Optional[date] = None) -> dict:
    """採点してから測る。日次ジョブの一段。**例外を投げない。**"""
    today = today or clock.today()
    try:
        scored = repository.score_predictions(today.isoformat())
    except Exception:
        logger.exception("scoring failed")
        return {"scored": 0, "error": "scoring failed"}

    result = measure(today)
    result["scored"] = scored
    result["scored_total"] = repository.count_scored_predictions()
    return result
