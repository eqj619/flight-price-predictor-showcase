"""買い時（底値の時期）の当否を、出発日を迎えた系列で答え合わせする。

**このアプリの目的は「いま底値の時期か」を言い当てること**（docs/issues.md 冒頭）。
2026-10-12 まで、ダッシュボードの精度欄は「明日の価格を何%で当てたか」しか
出しておらず、目的とずれていた（利用者の指摘）。ここで測るのは3つ。

1. 実際の底値は、出発の何日前だったか
2. 出発の◯日前に買っていたら、底値より何%高く払ったか
   — アプリの「値上がりが始まる日（出発52日前）までに買う」は、この表の1行
3. アプリの判定（今買う／待つ）は当たったか — verdict_log が貯まってから
4. アプリが予測した底値の日は当たったか、その日に買ったら得だったか
   — 同じく verdict_log から（2026-10-12 追加）

**集計だけを返す。** トークンも IP ハッシュも含めない（ダッシュボードは
接頭辞を知っていれば誰でも読める S3 に置く）。
"""
from __future__ import annotations

import statistics as st
from datetime import date
from typing import Optional

from app import clock
from app.db import repository
from app.predict.curve import (JAPAN_AIRPORTS, JP_TO_US_WEST, US_WEST_AIRPORTS,
                               CurveParams, load_params, route_params)

# 底値を語れる系列の条件。出発の60日前から7日前まで通して観測できていること。
# 観測の始まりより前に底があった系列は、底値の位置が分からない（打ち切り）。
FULL_FROM_DAYS = 60
FULL_TO_DAYS = 7
MIN_POINTS = 30

# 「出発の◯日前に買ったら」を測る日。84日前は観測の始まりの中央値あたり。
# 65 は日本発→米西海岸の期限（2026-10-14〜）。
BUY_DAYS = (84, 75, 65, 60, 55, 52, 45, 30, 21, 14, 7)

# 「底値に近い」とみなす幅。これ以内で買えたら当たり。
NEAR = 0.05

# 「待つ」が当たったとみなす下げ幅。誤差の範囲の上下で「下がった」と言わない。
WAIT_DROP = 0.02

# 答え合わせに数える判定は、出発のこの日数より前に出したものだけ。
# **出発当日・前日の判定には「その後」がほぼ無い。** 待つは必ず外れ、差は
# 必ず 0% になり、当たり外れの数字を意味の無い値で埋める（2026-10-17、
# 記録を始めた直後の 36件がすべてこれだった）。
MIN_GRADE_DBD = 2

# 買う日の価格は、その日の観測が無ければ前後この日数までの観測で代える。
TOLERANCE_DAYS = 2


def evaluate(today: Optional[date] = None,
             params: Optional[CurveParams] = None) -> dict:
    """ダッシュボードの「買い時の検証」欄に渡す集計。"""
    today = today or clock.today()
    params = params or load_params()
    paths = repository.completed_one_way_price_paths(today.isoformat())
    full = {k: s for k, s in paths.items()
            if max(s) >= FULL_FROM_DAYS and min(s) <= FULL_TO_DAYS
            and len(s) >= MIN_POINTS}

    rise = params.sweet_spot_start_days
    valley = (params.sweet_spot_start_days, params.sweet_spot_end_days)
    buy_days = _buy_days(full)
    scored = [b for b in buy_days if b["n"]]
    return {
        "as_of": today.isoformat(),
        "series": len(full),
        "series_all": len(paths),
        "departures": ([min(k[2] for k in full), max(k[2] for k in full)]
                       if full else None),
        "obs_start_median": (round(st.median(max(s) for s in full.values()))
                             if full else None),
        "rise_dbd": rise,
        "valley": list(valley),
        "near_pct": round(NEAR * 100),
        "bottoms": _bottoms(full, valley),
        "buy_days": buy_days,
        "best_dbd": (min(scored, key=lambda b: b["mean_pct"])["dbd"]
                     if scored else None),
        "verdicts": _verdicts(paths),
        "by_direction": _by_direction(full, paths, params),
        "timing": _timing(paths, rise),
    }


def _price_at(series: dict[int, float], dbd: int) -> Optional[float]:
    for off in range(TOLERANCE_DAYS + 1):
        for cand in (dbd + off, dbd - off):
            if cand in series:
                return series[cand]
    return None


def _bottom_day(series: dict[int, float]) -> int:
    """底値の日（出発までの日数）。

    **同じ最安が何日も続いたら、最後の日を採る。** 底は一点ではなく期間で
    あることが多く（2026-10-12、72系列中36系列で3日以上続いた）、最後の日が
    「その値段で買えた最後の機会」になる。
    """
    low = min(series.values())
    return min(d for d, p in series.items() if p <= low * 1.0001)


def _bottoms(full: dict, valley: tuple[int, int]) -> list[dict]:
    """底値の位置の分布。アプリの言う谷（valley）を1つの区切りとして出す。"""
    lo, hi = valley
    # 谷の位置は方向で違う（日本発 65〜75日前）。区切りが重ならないよう谷から作る。
    # 谷の下が25日より広ければ2つに割る（1本の棒に多くを混ぜない）。
    cuts = [(76, None, "76+")]
    if hi < 75:
        cuts.append((hi + 1, 75, f"{hi + 1}-75"))
    cuts.append((lo, hi, f"{lo}-{hi}"))
    if lo - 31 > 25:
        cuts += [(46, lo - 1, f"46-{lo - 1}"), (31, 45, "31-45")]
    else:
        cuts.append((31, lo - 1, f"31-{lo - 1}"))
    cuts += [
        (15, 30, "15-30"),
        (8, 14, "8-14"),
        (0, 7, "0-7"),
    ]
    days = [_bottom_day(s) for s in full.values()]
    out = []
    for a, b, label in cuts:
        n = sum(1 for d in days if d >= a and (b is None or d <= b))
        out.append({"label": label, "lo": a, "hi": b, "n": n,
                    "valley": (a, b) == valley})
    return out


def _buy_days(full: dict) -> list[dict]:
    """出発の◯日前に買ったら、底値より何%高く払ったか。"""
    out = []
    for dbd in BUY_DAYS:
        regrets = []
        for series in full.values():
            if max(series) < dbd:
                continue           # その日はまだ観測していない
            price = _price_at(series, dbd)
            if price is None:
                continue
            regrets.append(price / min(series.values()) - 1)
        if regrets:
            out.append({
                "dbd": dbd, "n": len(regrets),
                "median_pct": round(100 * st.median(regrets), 1),
                "mean_pct": round(100 * st.mean(regrets), 1),
                "within_pct": round(100 * sum(1 for r in regrets if r <= NEAR)
                                    / len(regrets)),
            })
        else:
            out.append({"dbd": dbd, "n": 0, "median_pct": None,
                        "mean_pct": None, "within_pct": None})
    return out


def _verdicts(paths: dict, match=None) -> dict:
    """アプリの判定は当たったか。match があれば、その方向の便だけ数える。

    **判定した日から後だけを見る。** 過去には戻って買えないので、比べる相手は
    「その日以降に買えた最安」。

        今買う  当たり = その後に、5%以上安い値段が出なかった
        待つ    当たり = その後に、2%以上安い値段が実際に出た
        様子見  当否は付けない（約束をしていない）。後で下がった割合だけ出す
    """
    rows = repository.verdicts_for_departed_routes(clock.today().isoformat())
    since = repository.first_verdict_date()
    stats: dict[str, dict] = {}
    flights = set()
    for row in rows:
        if row["days_before_departure"] < MIN_GRADE_DBD:
            continue
        if match and not match(row["origin"], row["destination"]):
            continue
        key = (row["origin"], row["destination"], row["departure_date"])
        series = paths.get(key)
        if not series:
            continue
        dbd = row["days_before_departure"]
        price = series.get(dbd) or row["current_price_usd"]
        later = [p for d, p in series.items() if d < dbd]
        if not price:
            continue
        best_after = min(later) if later else None
        flights.add(key)
        bucket = stats.setdefault(row["verdict"], {"n": 0, "hit": 0, "regrets": []})
        bucket["n"] += 1
        remaining_low = min([price] + later)
        bucket["regrets"].append(price / remaining_low - 1)
        if row["verdict"] == "buy_now":
            bucket["hit"] += int(best_after is None or best_after >= price * (1 - NEAR))
        else:
            bucket["hit"] += int(best_after is not None
                                 and best_after < price * (1 - WAIT_DROP))

    by_verdict = {}
    for verdict, b in stats.items():
        by_verdict[verdict] = {
            "n": b["n"],
            "hit_pct": round(100 * b["hit"] / b["n"]) if b["n"] else None,
            "median_regret_pct": (round(100 * st.median(b["regrets"]), 1)
                                  if b["regrets"] else None),
        }
    # flights: 便の本数。**件数は同じ便の毎日の判定を重ねて数えるので、独立した
    # 標本の数ではない。** 当たりの割合の確からしさは便の本数のほうで見る。
    return {"since": since, "evaluable": sum(b["n"] for b in stats.values()),
            "flights": len(flights), "by_verdict": by_verdict}


# 予測した底値の日が「当たった」とみなす幅（日）。
DAY_TOLERANCE = 7


def _timing(paths: dict, rise_dbd: int, match=None) -> dict:
    """アプリが予測した底値の日は当たったか。その日に買ったら得だったか。
    match があれば、その方向の便だけ数える。

    判定の記録（verdict_log）には毎朝、予測した底値の日と価格が残る。出発後に
    実際の価格と突き合わせて、3つを測る。

    1. **底値の日のずれ** — 予測した日と、実際の底値の日の差（日）。
       ＋は予測が実際より早すぎ（出発から遠すぎ）、−は遅すぎ
    2. **予測した日に買ったときの差** — 底値より何%高く払ったか
    3. **単純な買い方との比較** — 判定の日にすぐ買う／アプリの期限
       （出発 rise_dbd 日前）に買う、の2つ。**予測が役に立っているなら、
       この2つより差が小さいはず。**

    **比べる相手は、判定した日より後に買えた最安**（過去には戻って買えない）。
    予測した日が判定の日より前（過去）なら、「今買う」と言ったのと同じなので
    判定の日に買ったものとして扱う。

    比較の相手に「55日前に買う」を使わないのは、55日前は同じデータから
    選んだ日だから（自分の答えを見て決めた基準と比べることになる）。
    """
    rows = repository.verdicts_for_departed_routes(clock.today().isoformat())
    errs, app, now, deadline, wins = [], [], [], [], 0
    pred_dbds, keys = [], set()
    for row in rows:
        if not row.get("predicted_min_date"):
            continue
        if row["days_before_departure"] < MIN_GRADE_DBD:
            continue
        if match and not match(row["origin"], row["destination"]):
            continue
        key = (row["origin"], row["destination"], row["departure_date"])
        series = paths.get(key)
        if not series:
            continue
        v_dbd = row["days_before_departure"]
        price = series.get(v_dbd) or row["current_price_usd"]
        if not price:
            continue
        ahead = {d: p for d, p in series.items() if d <= v_dbd}
        ahead[v_dbd] = price
        low = min(ahead.values())
        actual = _bottom_day(ahead)

        dep = date.fromisoformat(row["departure_date"])
        pred = (dep - date.fromisoformat(row["predicted_min_date"])).days
        pred = max(0, min(pred, v_dbd))
        at_pred = _price_at(ahead, pred)
        at_deadline = _price_at(ahead, min(rise_dbd, v_dbd))
        if at_pred is None or at_deadline is None:
            continue

        keys.add(key)
        pred_dbds.append(pred)
        errs.append(pred - actual)
        app.append(at_pred / low - 1)
        now.append(price / low - 1)
        deadline.append(at_deadline / low - 1)
        wins += int(at_pred < at_deadline * 0.995)

    n = len(errs)
    if not n:
        return {"n": 0, "series": 0, "tolerance_days": DAY_TOLERANCE,
                "rise_dbd": rise_dbd}

    def pct(xs):
        return {"median": round(100 * st.median(xs), 1),
                "mean": round(100 * st.mean(xs), 1),
                "near": round(100 * sum(1 for x in xs if x <= NEAR) / len(xs))}

    q = st.quantiles(pred_dbds, n=4) if len(pred_dbds) > 1 else [pred_dbds[0]] * 3
    return {
        "n": n, "series": len(keys),
        "tolerance_days": DAY_TOLERANCE, "rise_dbd": rise_dbd,
        "day_err_median": round(st.median(errs), 1),
        "day_abs_err_median": round(st.median(abs(e) for e in errs), 1),
        "within_pct": round(100 * sum(1 for e in errs if abs(e) <= DAY_TOLERANCE) / n),
        "early_pct": round(100 * sum(1 for e in errs if e > DAY_TOLERANCE) / n),
        "late_pct": round(100 * sum(1 for e in errs if e < -DAY_TOLERANCE) / n),
        # 予測した日の散らばり（出発までの日数の四分位）。**ほぼ一点に集まって
        # いたら、予測は便ごとの予測ではなく固定の決まりに近い。**
        "pred_dbd_q": [round(q[0]), round(q[1]), round(q[2])],
        "regret": {"app": pct(app), "now": pct(now), "deadline": pct(deadline)},
        "beat_deadline_pct": round(100 * wins / n),
    }


# 方向別に出す区分。**課題16**: 底値の時期は日本発と米国発で違う。
US_WEST_TO_JP = "US-West->JP"
_DIRECTIONS = (
    (JP_TO_US_WEST, ("NRT", "LAX"),
     lambda o, d: o in JAPAN_AIRPORTS and d in US_WEST_AIRPORTS),
    (US_WEST_TO_JP, ("LAX", "NRT"),
     lambda o, d: o in US_WEST_AIRPORTS and d in JAPAN_AIRPORTS),
)


def _by_direction(full: dict, paths: dict, params: CurveParams) -> list[dict]:
    """方向別の底値の位置、「◯日前に買ったら」、判定と予測した日の答え合わせ。

    期限（rise_dbd）と谷（valley）はその方向に当てている値（curve.route_params）。
    valley_observed は谷の初めの日から観測できた系列の数。**これが series より
    少ないと、谷より前の底は見えないので「遅く来る」側に偏って見える。**
    """
    out = []
    for name, sample, match in _DIRECTIONS:
        sub = {k: s for k, s in full.items() if match(k[0], k[1])}
        buy_days = _buy_days(sub)
        scored = [b for b in buy_days if b["n"]]
        own = route_params(*sample, params)
        rise = own.sweet_spot_start_days
        valley = (rise, own.sweet_spot_end_days)
        out.append({
            "direction": name, "series": len(sub),
            "rise_dbd": rise,
            "valley": list(valley),
            "valley_observed": sum(1 for s in sub.values() if max(s) >= valley[1]),
            "bottoms": _bottoms(sub, valley),
            "buy_days": buy_days,
            "best_dbd": (min(scored, key=lambda b: b["mean_pct"])["dbd"]
                         if scored else None),
            "verdicts": _verdicts(paths, match),
            "timing": _timing(paths, rise, match),
        })
    return out
