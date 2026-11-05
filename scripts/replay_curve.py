"""曲線のパラメータを変えたら精度がどうなるかを、過去データで試す。

    python -m scripts.replay_curve --csv snapshots.csv
    python -m scripts.replay_curve --csv snapshots.csv --params new.json

**本番に入れる前にこれを通す。** 課題9で入れた計測は「入れたあと」しか
分からない。悪化させてから気づくのでは、利用者に外れた予測を見せた分が
戻らない。

## やっていること

過去のある日 `predicted_on` に立って、**その日までの観測だけ**で較正し、
`target` 日の価格を予測して、実際の値と比べる。engine と同じ関数
（`calibrate` と `price_shape`）を呼ぶので、本番と同じ計算になる。

## 注意

**出発までの日数ごとに分けて見ること。** まとめた数字は出発が遠い予測
（曲線が平坦で当てるのが容易）に薄まり、直前の誤差が見えなくなる
（2026-08-24 の実測で全体 0.5% に対し出発14日以内は 20.0%）。
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics as st
from collections import defaultdict
from datetime import date
from pathlib import Path

from app.config import get_settings
from app.predict.curve import CurveParams, DEFAULT_PARAMS
from app.predict.engine import calibrate
from app.predict.simulator import price_shape

# 予測を出す地平線。本番の PREDICTION_LOG_HORIZON_DAYS に合わせる。
HORIZONS = (1, 3, 7, 14, 30)


def load_series(path: Path) -> dict[tuple, list[dict]]:
    """CSV を便ごとの系列にまとめる。"""
    series: dict[tuple, list[dict]] = defaultdict(list)
    for row in csv.DictReader(path.open(encoding="utf-8")):
        key = (row["origin"], row["destination"], row["departure_date"],
               row["return_date"], int(row["stops"]), row["via_airport"],
               row["airline"])
        series[key].append({"check_date": row["check_date"],
                            "price_usd": float(row["price_usd"])})
    for rows in series.values():
        rows.sort(key=lambda r: r["check_date"])
    return series


def replay(series: dict[tuple, list[dict]], params: CurveParams,
           window: int) -> list[dict]:
    """各系列・各時点で予測を出し、実測と突き合わせる。

    **その日までの観測しか使わない。** 未来を見て較正すると、実運用では
    ありえない精度が出る。
    """
    out = []
    for key, rows in series.items():
        origin, destination, departure_date, return_date, stops, via, airline = key
        departure = date.fromisoformat(departure_date)
        by_date = {r["check_date"]: r["price_usd"] for r in rows}

        for i in range(1, len(rows)):
            history = rows[:i]                  # predicted_on までの観測
            predicted_on = date.fromisoformat(history[-1]["check_date"])
            scale, dispersion = calibrate(history, origin, destination,
                                          departure, stops, airline,
                                          params, window)
            for horizon in HORIZONS:
                target = predicted_on.toordinal() + horizon
                if target > departure.toordinal():
                    continue
                target_date = date.fromordinal(target)
                actual = by_date.get(target_date.isoformat())
                if actual is None or actual <= 0:
                    continue
                shape = price_shape(origin, destination, stops, airline,
                                    departure, target_date, params)
                if shape is None:
                    continue
                predicted = shape * scale
                out.append({
                    "predicted": predicted,
                    "actual": actual,
                    "lead_days": horizon,
                    "days_before_departure": (departure - target_date).days,
                    "observed_points": len(history),
                })
    return out


_BUCKETS = ((14, "0-14"), (30, "15-30"), (60, "31-60"), (120, "61-120"))


def _bucket(days_before: int) -> str:
    for limit, label in _BUCKETS:
        if days_before <= limit:
            return label
    return "121+"


def summarize(results: list[dict]) -> dict:
    if not results:
        return {"n": 0}
    errors = [(r["predicted"] - r["actual"]) / r["actual"] * 100 for r in results]
    return {
        "n": len(results),
        "abs": round(st.median(abs(e) for e in errors), 1),
        "bias": round(st.median(errors), 1),
    }


def report(label: str, results: list[dict]) -> None:
    print(f"\n=== {label} ===")
    overall = summarize(results)
    print(f"  全体          n={overall['n']:>6}  絶対誤差 {overall['abs']:>5}%  "
          f"偏り {overall['bias']:>+6}%")
    by = defaultdict(list)
    for r in results:
        by[_bucket(r["days_before_departure"])].append(r)
    for label_ in ("0-14", "15-30", "31-60", "61-120", "121+"):
        if label_ not in by:
            continue
        s = summarize(by[label_])
        print(f"  {label_:>7}日前  n={s['n']:>6}  絶対誤差 {s['abs']:>5}%  "
              f"偏り {s['bias']:>+6}%")


def load_params_file(path: Path) -> CurveParams:
    from dataclasses import replace
    raw = json.loads(path.read_text(encoding="utf-8"))
    return replace(DEFAULT_PARAMS, **raw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, required=True,
                    help="snapshots を書き出した CSV")
    ap.add_argument("--params", type=Path,
                    help="比較したいパラメータの JSON（省略すると現行のみ）")
    args = ap.parse_args()

    window = get_settings().calibration_window
    series = load_series(args.csv)
    print(f"系列 {len(series)}  較正窓 {window}日")

    report("現行のパラメータ", replay(series, DEFAULT_PARAMS, window))
    if args.params:
        report(f"変更案 ({args.params.name})",
               replay(series, load_params_file(args.params), window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
