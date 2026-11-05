"""「14日以内に5%以上下がるか」を当てる機械学習を、**影で**動かす（2026-10-23）。

## なぜ影なのか

出発日を迎えた約100便で当て直すと、勾配ブースティングは「すぐ買う」より
底値との差を 3〜13 ポイント縮めた（学習と確かめを入れ替えても同じ向き）。
「下がるか」の当たり具合は AUC 0.69。**ただし差の95%信頼区間は多くの条件で
0 をまたぎ**、閑散期の1シーズンしか無い（docs/issues.md 課題17）。

そこで利用者には見せず、毎朝の判定の記録（verdict_log）に確率だけ残す。
出発日を迎えた便から、今の判定と同じ物差しで比べられるようになる。
助言に使うかは 2027-01 中旬（年末年始の便が出発し終わった後）に決める。

## 作法

- **判定の記録を止めない。** scikit-learn が無い・学習に失敗した・特徴量が
  作れない、どれでも None を返して先へ進む
- 材料は自前の実測（snapshots）だけ。取得元の過去価格は混ぜない（課題16）
- 学習は出発日を迎えた片道の便だけ。**未来を見ない** — ラベルはその日より
  後の14日の価格で決まるので、出発済みの便なら必ず確定している
- モデルは自分で書き出したファイルだけを読む（pickle）。置き場所は DB と同じ
  フォルダ。アプリのフォルダは配備の rsync --delete で消える
"""
from __future__ import annotations

import logging
import pickle
import statistics as st
from datetime import date
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.db import repository
from app.predict.curve import JAPAN_AIRPORTS, US_WEST_AIRPORTS

logger = logging.getLogger(__name__)

DROP = 0.05          # 5%以上
HORIZON = 14         # 14日以内
MAX_DBD = 120        # これより遠い日は学習にも予測にも使わない（実測が薄い）
MODEL_VERSION = "gbm-drop14-v1"

FEATURE_NAMES = (
    "days_before", "direction", "vs_min14", "vs_mean14", "vs_min_all", "vs_max_all",
    "chg_1d", "chg_7d", "vol14", "n_airlines", "nonstop_ratio",
    "dep_weekday", "dep_month", "n_obs7",
)


def _direction(origin: str, destination: str) -> int:
    if origin in JAPAN_AIRPORTS and destination in US_WEST_AIRPORTS:
        return 0
    if origin in US_WEST_AIRPORTS and destination in JAPAN_AIRPORTS:
        return 1
    return 2


def features(key: tuple[str, str, str], s: dict[int, tuple], t: int) -> list[float]:
    """出発 t 日前の時点で**見えている情報だけ**で作る。

    s は {出発までの日数: (その日の最安, 航空会社の数, 直行の最安 or None)}。
    t より大きい日数が過去。
    """
    origin, destination, dep = key
    p = s[t][0]
    past14 = [s[x][0] for x in s if t <= x <= t + 14]
    past7 = [x for x in s if t <= x <= t + 7]
    allpast = [s[x][0] for x in s if x >= t]
    prev1 = next((s[x][0] for x in range(t + 1, t + 4) if x in s), p)
    prev7 = next((s[x][0] for x in range(t + 7, t + 10) if x in s), p)
    xs = sorted((x for x in s if t <= x <= t + 14), reverse=True)
    rets = [s[b][0] / s[a][0] - 1 for a, b in zip(xs, xs[1:])]
    dep_d = date.fromisoformat(dep)
    nonstop = s[t][2]
    return [
        t, _direction(origin, destination),
        p / min(past14), p / st.mean(past14), p / min(allpast), p / max(allpast),
        p / prev1 - 1, p / prev7 - 1,
        st.pstdev(rets) if len(rets) > 1 else 0.0,
        s[t][1] or 0, (nonstop / p) if nonstop else 3.0,
        dep_d.weekday(), dep_d.month, len(past7),
    ]


def label(s: dict[int, tuple], t: int) -> Optional[int]:
    """t 日前より後（出発に近い側）14日以内に、5%以上安い日があったか。後が無ければ None。"""
    future = [s[x][0] for x in s if t - HORIZON <= x < t]
    if not future:
        return None
    return int(min(future) < s[t][0] * (1 - DROP))


def training_rows(series: dict) -> tuple[list[list[float]], list[int], int]:
    X, y, used = [], [], set()
    for key, s in series.items():
        if len(s) < 15:
            continue
        for t in s:
            if not 1 <= t <= MAX_DBD:
                continue
            yy = label(s, t)
            if yy is None:
                continue
            X.append(features(key, s, t)); y.append(yy); used.add(key)
    return X, y, len(used)


def _model_path() -> Path:
    settings = get_settings()
    return Path(settings.ml_model_path or Path(settings.db_path).parent / "drop_model.pkl")


def train(today: Optional[date] = None) -> Optional[dict]:
    """出発日を迎えた便で学習し、ファイルに書き出す。足りなければ None。"""
    from sklearn.ensemble import HistGradientBoostingClassifier

    settings = get_settings()
    today = today or date.today()
    series = repository.one_way_daily_rows(departed_before=today.isoformat())
    X, y, n_series = training_rows(series)
    if n_series < settings.ml_min_series or len(set(y)) < 2:
        logger.info("影の機械学習: 学習を見送り（出発済みの便 %d 本、%d 行）", n_series, len(y))
        return None
    model = HistGradientBoostingClassifier(
        max_depth=3, max_iter=200, learning_rate=0.05,
        min_samples_leaf=40, random_state=0).fit(X, y)
    bundle = {
        "model": model, "version": MODEL_VERSION, "trained_on": today.isoformat(),
        "n_series": n_series, "n_rows": len(y), "base_rate": sum(y) / len(y),
        "features": FEATURE_NAMES,
    }
    path = _model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as f:
        pickle.dump(bundle, f)
    tmp.replace(path)               # 書きかけのファイルを読ませない
    logger.info("影の機械学習: 学習した（便 %d 本、%d 行、下がった割合 %.2f）",
                n_series, len(y), bundle["base_rate"])
    return bundle


def load() -> Optional[dict]:
    path = _model_path()
    if not path.exists():
        return None
    with path.open("rb") as f:
        return pickle.load(f)


def ensure_model(today: date) -> Optional[dict]:
    """モデルが無いか古ければ学習し直す。失敗しても例外を出さない。"""
    settings = get_settings()
    try:
        bundle = load()
        stale = (bundle is None or bundle.get("version") != MODEL_VERSION
                 or (today - date.fromisoformat(bundle["trained_on"])).days
                 >= settings.ml_retrain_days)
        if stale:
            bundle = train(today) or bundle
        return bundle
    except Exception:
        logger.exception("影の機械学習: モデルを用意できなかった")
        return None


def shadow_probabilities(today: date) -> tuple[dict, Optional[str]]:
    """今日の各片道ルートについて「14日以内に5%以上下がる確率」。

    戻り値は ({(出発地, 到着地, 出発日): 確率}, モデルの版)。今日の実測が
    無いルート、出発まで MAX_DBD 日より遠いルートは入らない。
    """
    if not get_settings().ml_shadow_enabled:
        return {}, None
    bundle = ensure_model(today)
    if bundle is None:
        return {}, None
    try:
        series = repository.one_way_daily_rows(departing_from=today.isoformat())
        keys, rows = [], []
        for key, s in series.items():
            t = (date.fromisoformat(key[2]) - today).days
            if t in s and 0 <= t <= MAX_DBD:
                keys.append(key); rows.append(features(key, s, t))
        if not rows:
            return {}, bundle["version"]
        probs = bundle["model"].predict_proba(rows)[:, 1]
        tag = f"{bundle['version']}@{bundle['trained_on']}"
        return {k: round(float(p), 4) for k, p in zip(keys, probs)}, tag
    except Exception:
        logger.exception("影の機械学習: 予測できなかった")
        return {}, None
