"""以前は便が取れていたルートの0件を、間を置いて引き直す（2026-10-12）。

**なぜ要るか。** 2026-10-01 ごろから、収集の最中にだけ0件が増えた
（9月 1〜5% → 10月 15〜23%）。羽田→ロサンゼルスのような路線まで空で返り、
約8割は翌日に戻る。同じ検索を収集の時間帯以外に本番サーバから流すと
便が取れた。取得失敗には出ないので、**静かにデータが欠けていた。**

ここで確かめるのは:

1. 引き直して取れたら、その日のデータとして残り、0件の記録は残らない
2. 引き直しても0件なら、記録は1回だけ（二重に積まない）。理由も残る
3. **一度も取れたことの無いルートは引き直さない**（予約受付前など、
   0件が正常。引き直しても無駄な検索が増えるだけ）
4. 設定で止められる
"""
from datetime import date, timedelta

import pytest

from app.collect import collector
from app.collect.collector import collect_once
from app.collect.provider import PriceProvider, Quote
from app.config import get_settings
from app.db import repository
from app.db.connection import get_connection

CHECK = date(2026, 10, 12)
DEP = "2026-12-20"


class FlakyProvider(PriceProvider):
    """ルートごとに「何回目の呼び出しから便を返すか」を決められる偽物。

    1ルートには経由回数ごとに複数のクエリが飛ぶので、**ルート単位の巡回回数**
    で数える（stops=0 の呼び出しが来たら、そのルートの新しい巡回とみなす）。
    """
    name = "flaky"

    def __init__(self, empty_rounds: dict, reason="error_status"):
        self.empty_rounds = empty_rounds      # (o, d) -> 0件で返す巡回の回数
        self.rounds: dict = {}
        self.calls: list = []
        self.reason = reason
        self.last_empty_reason = None

    def fetch(self, origin, destination, departure_date, stops,
              airlines=None, return_date=None):
        key = (origin, destination)
        if stops == 0 and not airlines:
            self.rounds[key] = self.rounds.get(key, 0) + 1
        self.calls.append((key, stops, tuple(airlines or ())))
        self.last_empty_reason = None
        if self.rounds.get(key, 1) <= self.empty_rounds.get(key, 0):
            self.last_empty_reason = self.reason
            return []
        if stops != 1 or airlines:
            self.last_empty_reason = "empty_page"
            return []
        return [Quote(airline="ANA", stops=1, via_airport="LAX", price_usd=700.0)]


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    """実際には待たない。待とうとした秒数だけ記録する。"""
    waited = []
    monkeypatch.setattr(collector, "_sleep", lambda s: waited.append(s))
    return waited


def _route(o, d):
    return repository.upsert_monitored_route(o, d, DEP)


def _history(o, d):
    """以前に便が取れていた、を作る（前日の実測）。"""
    repository.upsert_snapshots([repository.Snapshot(
        check_date=(CHECK - timedelta(days=1)).isoformat(), departure_date=DEP,
        origin=o, destination=d, stops=1, via_airport="LAX", airline="ANA",
        price_usd=690.0, source="real", fetched_at="2026-10-11T03:00:00")])


def _rows(sql, *args):
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def _empties(o, d):
    return _rows("SELECT stops, reason FROM empty_searches "
                 "WHERE origin=? AND destination=? AND check_date=? ORDER BY stops",
                 o, d, CHECK.isoformat())


def _today_prices(o, d):
    return _rows("SELECT price_usd FROM snapshots "
                 "WHERE origin=? AND destination=? AND check_date=?",
                 o, d, CHECK.isoformat())


def test_an_established_route_that_comes_back_on_retry_keeps_its_data(no_wait):
    """**これが直したかったこと。** 収集中だけ空だったルートが、その日の実測を持つ。"""
    _route("HND", "LAX"); _history("HND", "LAX")
    provider = FlakyProvider({("HND", "LAX"): 1})     # 1巡目だけ空

    summary = collect_once(provider, CHECK)

    assert _today_prices("HND", "LAX") == [{"price_usd": 700.0}]
    assert _empties("HND", "LAX") == [], "回復したのに0件が記録されている"
    assert summary["empty_retried"] == 1
    assert summary["empty_recovered"] == 1
    assert no_wait == [get_settings().empty_retry_delay_seconds], "間を置かずに引き直した"


def test_still_empty_after_retry_is_recorded_once_with_its_reason():
    """引き直しても0件なら記録する。**二重に積まない。** 理由も残す。"""
    _route("HND", "LAX"); _history("HND", "LAX")
    provider = FlakyProvider({("HND", "LAX"): 99})    # ずっと空

    summary = collect_once(provider, CHECK)

    rows = _empties("HND", "LAX")
    stops = [r["stops"] for r in rows]
    assert len(stops) == len(set(stops)), f"同じ経由回数が二重に記録された: {stops}"
    assert rows and all(r["reason"] == "error_status" for r in rows)
    assert summary["empty_recovered"] == 0
    assert summary["empty_first_pass_reasons"].get("error_status")


def test_a_route_that_never_had_flights_is_not_retried(no_wait):
    """**予約受付前などは0件が正常。** 引き直しても無駄な検索が増えるだけ。"""
    _route("SAN", "NRT")                                # 履歴なし
    provider = FlakyProvider({("SAN", "NRT"): 99}, reason="empty_page")

    summary = collect_once(provider, CHECK)

    assert provider.rounds[("SAN", "NRT")] == 1, "履歴の無いルートを引き直した"
    assert summary["empty_retried"] == 0
    assert no_wait == [], "引き直す対象が無いのに待った"
    assert _empties("SAN", "NRT"), "0件の記録が残っていない"


def test_a_route_with_flights_is_left_alone():
    _route("HND", "LAX"); _history("HND", "LAX")
    provider = FlakyProvider({})                        # 最初から取れる

    summary = collect_once(provider, CHECK)

    assert provider.rounds[("HND", "LAX")] == 1
    assert summary["empty_retried"] == 0


def test_retry_can_be_switched_off(monkeypatch, no_wait):
    """**引き直しで状況が悪くなるなら止められる。**"""
    monkeypatch.setenv("EMPTY_RETRY_ENABLED", "false")
    get_settings.cache_clear()
    _route("HND", "LAX"); _history("HND", "LAX")
    provider = FlakyProvider({("HND", "LAX"): 1})

    summary = collect_once(provider, CHECK)

    assert provider.rounds[("HND", "LAX")] == 1
    assert summary["empty_retried"] == 0
    assert no_wait == []
    assert _empties("HND", "LAX"), "止めたのに0件が記録されていない"
    get_settings.cache_clear()


def test_the_reason_reaches_the_database_even_without_a_retry():
    """履歴の無いルートの0件も、理由つきで残る（計測の本体）。"""
    _route("SAN", "NRT")
    collect_once(FlakyProvider({("SAN", "NRT"): 99}, reason="error_status"), CHECK)

    reasons = {r["reason"] for r in _empties("SAN", "NRT")}
    assert reasons == {"error_status"}
