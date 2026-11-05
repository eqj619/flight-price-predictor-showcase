"""影の機械学習（2026-10-23、app/ml/drop_model.py）。

利用者には見せず、判定の記録に「14日以内に5%以上下がる確率」を残すだけ。
**判定の記録を止めないこと**と、**未来を見ないこと**を固定する。
"""
import random
from datetime import date, timedelta

import pytest

from app.config import get_settings
from app.db import repository
from app.ml import drop_model

TODAY = date(2026, 10, 23)


def _series(origin, destination, departure: date, prices: dict[int, float]):
    repository.upsert_snapshots([
        repository.Snapshot(
            check_date=(departure - timedelta(days=dbd)).isoformat(),
            departure_date=departure.isoformat(), origin=origin,
            destination=destination, stops=1, via_airport="LAX", airline="ANA",
            price_usd=p, source="real", fetched_at="2026-10-01T03:00:00")
        for dbd, p in prices.items()])


def _random_walk(seed, days=60, start=600.0):
    rng = random.Random(seed)
    p, out = start, {}
    for dbd in range(days, -1, -1):
        p *= 1 + rng.uniform(-0.08, 0.08)
        out[dbd] = round(p, 2)
    return out


@pytest.fixture
def few_series_ok(monkeypatch):
    monkeypatch.setenv("ML_MIN_SERIES", "3")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_label_only_looks_after_the_day():
    s = {20: (500, 3, None), 10: (470, 3, None), 30: (300, 3, None)}
    # 30日前の $300 は過去。後ろの14日（6〜19日前）にある $470 は 500 の 6% 安
    assert drop_model.label(s, 20) == 1
    assert drop_model.label(s, 10) is None        # 後が無い


def test_features_use_only_the_past():
    s = {d: (600.0 + d, 4, None) for d in range(0, 40)}
    a = drop_model.features(("NRT", "LAX", "2026-12-01"), s, 20)
    s2 = dict(s); s2[5] = (1.0, 4, None)            # 未来の日を極端に変える
    b = drop_model.features(("NRT", "LAX", "2026-12-01"), s2, 20)
    assert a == b


def test_train_and_predict(few_series_ok):
    for i in range(6):
        _series("NRT", "LAX", TODAY - timedelta(days=1 + i), _random_walk(i))
    _series("NRT", "LAX", TODAY + timedelta(days=30),
            {d: p for d, p in _random_walk(99, 70).items() if d >= 30})
    bundle = drop_model.train(TODAY)
    assert bundle and bundle["n_series"] == 6
    probs, tag = drop_model.shadow_probabilities(TODAY)
    key = ("NRT", "LAX", (TODAY + timedelta(days=30)).isoformat())
    assert 0.0 <= probs[key] <= 1.0
    assert tag.startswith(drop_model.MODEL_VERSION)


def test_too_little_data_skips_training():
    _series("NRT", "LAX", TODAY - timedelta(days=1), _random_walk(1))
    assert drop_model.train(TODAY) is None
    assert drop_model.shadow_probabilities(TODAY) == ({}, None)


def test_a_broken_model_never_stops_the_verdicts(few_series_ok, monkeypatch):
    """**判定の記録を止めない。** 学習でも予測でも、失敗したら None で先へ進む。"""
    def boom(*a, **k):
        raise RuntimeError("simulated")
    monkeypatch.setattr(drop_model, "train", boom)
    assert drop_model.shadow_probabilities(TODAY) == ({}, None)


def test_the_probability_lands_in_the_verdict_log(few_series_ok):
    from app.jobs.daily import _log_verdicts
    for i in range(6):
        _series("NRT", "LAX", TODAY - timedelta(days=1 + i), _random_walk(i))
    dep = (TODAY + timedelta(days=30)).isoformat()
    _series("NRT", "LAX", date.fromisoformat(dep),
            {d: p for d, p in _random_walk(7, 70).items() if d >= 30})
    repository.upsert_monitored_route("NRT", "LAX", dep)
    _log_verdicts(TODAY)
    with repository.get_connection() as conn:
        row = conn.execute("SELECT ml_drop_prob, ml_model FROM verdict_log "
                           "WHERE departure_date = ?", (dep,)).fetchone()
    assert row is not None
    assert row["ml_drop_prob"] is not None and 0 <= row["ml_drop_prob"] <= 1
    assert row["ml_model"].startswith(drop_model.MODEL_VERSION)


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("ML_SHADOW_ENABLED", "false")
    get_settings.cache_clear()
    assert drop_model.shadow_probabilities(TODAY) == ({}, None)
