"""買い方の当て直し（課題17）。出発日を迎えた片道の便で、底値より何%高く払ったかを比べる。

    DB_PATH=/path/to/flights.db python -m scripts.backtest_buy_policy --cutoff 2026-10-05

出発のS日前から見始めて出発までに1回買うとき、次の買い方を比べる。

- すぐ買う
- 決まった日に買う（何日前かは学習用の便で選ぶ）
- 機械学習（app/ml/drop_model.py と同じ特徴量）で「14日以内に5%以上下がる確率」が
  しきい値を下回った最初の日に買う（しきい値は学習用の便で選ぶ）

**出発日で学習と確かめを分ける**（cutoff より前の出発を学習）。同じ便の日どうしは
ほぼ同じ情報なので、日でランダムに分けると成績が良く見えすぎる。
`--swap` で学習と確かめを入れ替える。差には 95% のブートストラップ区間を付ける。

材料は自前の実測（snapshots）だけ。取得元の過去価格は混ぜない（課題16）。
"""
from __future__ import annotations

import argparse
from datetime import date

import numpy as np

from app.db import repository
from app.ml import drop_model


def simulate(s: dict, start: int, decide) -> float:
    """start 日前から毎日判断し、最初に「買う」と言った日に買う。最後の観測日には必ず買う。"""
    days = sorted((x for x in s if x <= start), reverse=True)
    low = min(s[x][0] for x in days)
    for t in days:
        if t == days[-1] or decide(t):
            return s[t][0] / low - 1
    raise AssertionError


def eligible(s: dict, start: int) -> bool:
    has_start = any(x in s for x in (start - 1, start, start + 1))
    dense = sum(1 for x in s if x <= start) >= 0.7 * (start + 1)
    return has_start and dense


def evaluate(series: dict, keys: list, start: int, decide_for) -> list[float]:
    out = []
    for key in keys:
        s = series[key]
        if eligible(s, start):
            out.append(simulate(s, start, decide_for(key, s)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--cutoff", required=True, help="これより前に出発した便で学習する")
    ap.add_argument("--swap", action="store_true", help="学習と確かめを入れ替える")
    ap.add_argument("--starts", default="45,30,21")
    args = ap.parse_args()

    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    series = repository.one_way_daily_rows(departed_before=args.today)
    early = [k for k in series if k[2] < args.cutoff]
    late = [k for k in series if k[2] >= args.cutoff]
    train_keys, test_keys = (late, early) if args.swap else (early, late)

    X, y, n_train = drop_model.training_rows({k: series[k] for k in train_keys})
    Xt, yt, n_test = drop_model.training_rows({k: series[k] for k in test_keys})
    model = HistGradientBoostingClassifier(max_depth=3, max_iter=200, learning_rate=0.05,
                                           min_samples_leaf=40, random_state=0).fit(X, y)
    print(f"学習: 便 {n_train} 本 / {len(y)} 行   確かめ: 便 {n_test} 本 / {len(yt)} 行")
    print(f"「14日以内に5%以上下がるか」AUC {roc_auc_score(yt, model.predict_proba(Xt)[:, 1]):.3f}"
          "（0.5 = 当てずっぽう）")

    def prob(key, s, t):
        return model.predict_proba([drop_model.features(key, s, t)])[0, 1]

    rng = np.random.default_rng(0)

    def ci(d):
        bs = [np.mean(rng.choice(d, len(d))) for _ in range(2000)]
        lo, hi = np.percentile(bs, [2.5, 97.5]) * 100
        return f"[{lo:+.1f}, {hi:+.1f}]"

    for start in (int(x) for x in args.starts.split(",")):
        fixed = min(range(start + 1), key=lambda fd: np.mean(
            evaluate(series, train_keys, start, lambda k, s, fd=fd: (lambda t: t <= fd)) or [9]))
        th = min(np.arange(0.05, 0.95, 0.05), key=lambda th: np.mean(
            evaluate(series, train_keys, start,
                     lambda k, s, th=th: (lambda t: prob(k, s, t) < th)) or [9]))
        now = np.array(evaluate(series, test_keys, start, lambda k, s: (lambda t: True)))
        fix = np.array(evaluate(series, test_keys, start, lambda k, s: (lambda t: t <= fixed)))
        ml = np.array(evaluate(series, test_keys, start,
                               lambda k, s: (lambda t: prob(k, s, t) < th)))
        if not len(now):
            print(f"\n{start}日前から: 確かめられる便が無い")
            continue
        print(f"\n{start}日前から（n={len(now)}）: すぐ買う +{100*now.mean():.1f}%  "
              f"決まった日({fixed}日前) +{100*fix.mean():.1f}%  機械学習(<{th:.2f}) +{100*ml.mean():.1f}%")
        print(f"  機械学習−すぐ買う {100*(ml-now).mean():+.1f}pt {ci(ml-now)}   "
              f"機械学習−決まった日 {100*(ml-fix).mean():+.1f}pt {ci(ml-fix)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
