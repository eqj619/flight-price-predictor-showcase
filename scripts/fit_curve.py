#!/usr/bin/env python3
"""`price_history` から価格曲線のパラメータを推定する。**開発時にだけ実行する。**

    .venv/bin/python scripts/fit_curve.py --db /path/to/flights.db \\
        --out data/curve_params.json

`app/predict/curve.py` の既定値は東京⇄米西海岸の観測に基づく初期値で、
統計的に推定されたものではない。ここで実測から引き直し、`CURVE_PARAMS_PATH`
が指す JSON に書き出す（`load_params()` が部分上書きで読む）。

**素朴に「系列ごとに自分の平均で割って重ねる」ことはしない。** 履歴は1系列
約61日ぶんしかなく、系列ごとに見えている区間が違う。自分の平均で割ると、
出発300日後の系列と出発20日後の系列で基準がそろわず、曲線の傾きに
系列構成の偏りがそのまま乗る。

代わりに **log 価格を「系列効果 + 出発までの日数の効果」に分解する**
（二元固定効果）。交互に推定して収束させると、区間が重なり合う系列どうしが
互いの水準を結び付け、どの系列も見ていない全体の形が復元される。

推定するのは**データがある範囲だけ**。出発15日前より内側は点が数十しかない
ので触らない（既定値のまま残す）。曲線の「高さ」は較正のスケール項に
吸収されるため、意味があるのは形だけ。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 推定を信じる範囲。下限はデータ量、上限は系列数が薄くなる点で切る。
FIT_MIN_DAYS, FIT_MAX_DAYS = 15, 200
# 1日あたりこれ未満の観測しかない日は、形の推定に使わない。
MIN_POINTS_PER_DAY = 8
ITERATIONS = 50


def load_series(db: Path | None, csv_path: Path | None) -> dict:
    """{(o,d,dep): [(出発まで日数, 価格), ...]}"""
    if csv_path:
        rows = list(csv.DictReader(csv_path.open()))
    else:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT origin, destination, departure_date, observed_date, price_usd "
            "FROM price_history")]

    series = defaultdict(list)
    for r in rows:
        dep = date.fromisoformat(r["departure_date"])
        obs = date.fromisoformat(r["observed_date"])
        price = float(r["price_usd"])
        if price <= 0:
            continue
        series[(r["origin"], r["destination"], r["departure_date"])].append(
            ((dep - obs).days, price))
    return series


def recover_shape(series: dict) -> dict[int, float]:
    """log 価格から系列効果を除き、出発までの日数ごとの相対倍率を返す。

    交互最小二乗。**平均ではなく中央値で更新する。** 便の入れ替わりで
    一日だけ跳ねる系列があり、平均だとその跳ねが曲線に漏れる。
    """
    obs = [(k, d, math.log(p)) for k, pts in series.items() for d, p in pts]
    offset: dict = {k: statistics.median([lp for kk, _, lp in obs if kk == k])
                    for k in series}
    shape: dict[int, float] = {}

    for _ in range(ITERATIONS):
        by_day = defaultdict(list)
        for k, d, lp in obs:
            by_day[d].append(lp - offset[k])
        shape = {d: statistics.median(v) for d, v in by_day.items()
                 if len(v) >= MIN_POINTS_PER_DAY}

        by_series = defaultdict(list)
        for k, d, lp in obs:
            if d in shape:
                by_series[k].append(lp - shape[d])
        new = {k: statistics.median(v) for k, v in by_series.items() if v}
        moved = max((abs(new[k] - offset[k]) for k in new), default=0.0)
        offset.update(new)
        if moved < 1e-6:
            break

    # 推定範囲の中で中心化する。曲線の高さは較正で吸収されるので、
    # ここで基準をどこに置くかは形に影響しない。
    core = [v for d, v in shape.items() if FIT_MIN_DAYS <= d <= FIT_MAX_DAYS]
    centre = statistics.median(core) if core else 0.0
    return {d: math.exp(v - centre) for d, v in shape.items()}


def model_curve(days: int, p: dict) -> float:
    """`app/predict/simulator.proximity_factor` と同じ形。

    **あちらを変えたらこちらも変える。** 別実装を持つのは、推定のたびに
    アプリを import して設定を読ませたくないため。
    """
    if p["sweet_spot_start_days"] <= days <= p["sweet_spot_end_days"]:
        return p["sweet_spot_factor"]
    if days > p["sweet_spot_end_days"]:
        extra = min((days - p["sweet_spot_end_days"]) / p["far_taper_days"], 1.0)
        return p["sweet_spot_factor"] + extra * p["far_taper_amount"]
    if days > p["ramp_end_days"]:
        span = p["sweet_spot_start_days"] - p["ramp_end_days"]
        progressed = (days - p["ramp_end_days"]) / span
        return p["ramp_top_factor"] - progressed * (
            p["ramp_top_factor"] - p["sweet_spot_factor"])
    # 直前需要の跳ね上がり。**本体と同じ式にしておくこと。**
    ramp = (p["ramp_end_days"] - days) / p["ramp_end_days"]
    return p["last_minute_start_factor"] + ramp * (
        p["last_minute_peak_factor"] - p["last_minute_start_factor"])


GRID = {
    "sweet_spot_start_days": range(30, 66, 2),
    "sweet_spot_end_days": range(50, 106, 3),
    "sweet_spot_factor": [round(0.90 + i * 0.005, 3) for i in range(21)],
    "far_taper_days": range(20, 161, 10),
    # **負値も探す。** 実測では出発165日前より前がむしろ安い。「底値帯より前は
    # 徐々に高くなる」という元の想定は、データに支持されていない。
    "far_taper_amount": [round(-0.06 + i * 0.005, 3) for i in range(27)],
    "ramp_end_days": range(14, 31, 2),
    "ramp_top_factor": [round(1.00 + i * 0.01, 3) for i in range(16)],
    # 出発15〜20日前の実測から辛うじて動かせる。**peak（当日）は触らない** —
    # 出発5日前より内側の観測が1点も無く、推定したふりになる。
    "last_minute_start_factor": [round(1.0 + i * 0.05, 2) for i in range(15)],
}


def fit(shape: dict[int, float], weights: dict[int, int], start: dict) -> dict:
    """座標降下でグリッドを1軸ずつ探す。

    素直な全探索は 18x19x21x15x15x9x16 ≒ 3.6 億通りで回らない。曲線は
    各軸について素直な形をしているので、1軸ずつ最良へ動かす操作を
    繰り返せば十分に収束する。
    """
    days = [d for d in shape if FIT_MIN_DAYS <= d <= FIT_MAX_DAYS]

    def loss(p):
        # log 空間の重み付き二乗誤差。価格比の誤差を対称に扱うため。
        return sum(weights[d] * (math.log(model_curve(d, p) / shape[d])) ** 2
                   for d in days)

    best = dict(start)
    best_loss = loss(best)
    for _ in range(12):
        improved = False
        for key, values in GRID.items():
            for v in values:
                if v == best[key]:
                    continue
                cand = dict(best, **{key: v})
                if cand["sweet_spot_start_days"] >= cand["sweet_spot_end_days"]:
                    continue
                if cand["ramp_end_days"] >= cand["sweet_spot_start_days"]:
                    continue
                l = loss(cand)
                if l < best_loss - 1e-12:
                    best, best_loss, improved = cand, l, True
        if not improved:
            break
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path)
    ap.add_argument("--csv", type=Path, help="price_history を書き出した CSV")
    ap.add_argument("--out", type=Path, help="推定値の書き出し先 JSON")
    args = ap.parse_args()
    if not args.db and not args.csv:
        ap.error("--db か --csv のどちらかが要る")

    series = load_series(args.db, args.csv)
    print(f"系列 {len(series)} 本 / 点 {sum(len(v) for v in series.values()):,}")

    shape = recover_shape(series)
    weights = defaultdict(int)
    for pts in series.values():
        for d, _ in pts:
            weights[d] += 1

    # **現行値はアプリから読む。** ここに直書きすると、curve.py を更新した
    # 次の実行で「現行」列が嘘になる。DEFAULT_PARAMS は素の dataclass なので
    # 設定ファイルも環境変数も読まない。
    import sys
    sys.path.insert(0, str(ROOT))
    from app.predict.curve import DEFAULT_PARAMS
    start = {k: getattr(DEFAULT_PARAMS, k)
             for k in list(GRID) + ["last_minute_peak_factor"]}
    fitted = fit(shape, weights, start)

    n_fit = sum(weights[d] for d in shape if FIT_MIN_DAYS <= d <= FIT_MAX_DAYS)
    print(f"\n推定範囲: 出発 {FIT_MIN_DAYS}–{FIT_MAX_DAYS} 日前（{n_fit:,} 点）")
    if n_fit == 0:
        # **黙って現行値を書き出さない。** 推定した値だと誤解される。
        print("推定範囲に観測が無い。何も推定していない。", file=sys.stderr)
        return 1
    print(f"{'項目':<24}{'現行':>10}{'推定':>10}")
    for k in GRID:
        print(f"  {k:<22}{start[k]:>10}{fitted[k]:>10}")

    print(f"\n{'出発何日前':>11} {'実測':>7} {'現行':>7} {'推定':>7}")
    for lo in range(15, 205, 10):
        ds = [d for d in shape if lo <= d < lo + 10]
        if not ds:
            continue
        m = statistics.median(shape[d] for d in ds)
        c = statistics.fmean(model_curve(d, start) for d in ds)
        f = statistics.fmean(model_curve(d, fitted) for d in ds)
        flag = "  ←" if abs(f - m) < abs(c - m) - 0.005 else ""
        print(f"  {lo:>3}–{lo+9:<3}日 {m:>7.3f} {c:>7.3f} {f:>7.3f}{flag}")

    if args.out:
        # **推定した項目だけ書く。** load_params は部分上書きなので、
        # 触っていない係数（季節性・航空会社）は既定値のまま残る。
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(fitted, indent=2) + "\n", encoding="utf-8")
        print(f"\n書き出し: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
