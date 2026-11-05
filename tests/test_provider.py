"""FastFlightsProvider の単体テスト。

既存 flight-price-monitor の運用で判明した3つの罠（TypeError の意味、
セグメント数の検証、リトライ）が壊れていないことを固定する。ネットワークは
叩かず、get_flights / create_query を差し替えて検証する。
"""
from datetime import date
from types import SimpleNamespace

import pytest

from app.collect import fast_flights_provider as ffp
from app.collect.provider import ProviderError

DEP = date(2026, 12, 20)


def _segment(arrival_code, dep=(2026, 10, 15), dep_t=(11, 30),
             arr=(2026, 10, 15), arr_t=(18, 45), duration=555,
             plane="Boeing 787"):
    return SimpleNamespace(
        to_airport=SimpleNamespace(code=arrival_code),
        from_airport=SimpleNamespace(code="XXX"),
        departure=SimpleNamespace(date=dep, time=dep_t),
        arrival=SimpleNamespace(date=arr, time=arr_t),
        duration=duration,
        plane_type=plane,
    )


def _group(price, airlines, segment_arrivals, segments=None, carbon=None):
    """fast_flights の Flights を模したオブジェクト。

    segment_arrivals は各セグメントの到着空港コード。
    直行便なら1要素、1経由なら2要素。
    """
    return SimpleNamespace(
        price=price,
        airlines=airlines,
        flights=segments if segments is not None
                else [_segment(c) for c in segment_arrivals],
        carbon=carbon,
    )


@pytest.fixture
def provider():
    # 実際に待つとリトライ経路のテストだけで十数秒かかる。
    return ffp.FastFlightsProvider(sleep=lambda _seconds: None)


# ------------------------------------------------------------------
# (1) TypeError は「該当便なし」
# ------------------------------------------------------------------

def test_type_error_means_no_results_not_an_error(provider, monkeypatch):
    def raise_type_error(_query, **_kwargs):
        raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setattr(ffp, "search_flights", raise_type_error)

    # 例外ではなく空リスト。ProviderError にすると、直行便が飛んでいない
    # 路線で fetch_errors が埋まって本当の障害が見えなくなる。
    assert provider.fetch("HND", "SAN", DEP, stops=0) == []


def test_flights_not_found_is_also_no_results(provider, monkeypatch):
    """fast_flights は「便が無い」を FlightsNotFound でも知らせてくる。

    これを一般の失敗として扱うと、250日先など在庫が出ていない路線で
    3回ずつリトライしたうえ fetch_errors に積まれ、本当の障害が埋もれる。
    """
    from fast_flights.exceptions import FlightsNotFound

    calls = []

    def not_found(query, **_kwargs):
        calls.append(query)
        raise FlightsNotFound("no flights found")

    monkeypatch.setattr(ffp, "search_flights", not_found)

    assert provider.fetch("SAN", "HND", DEP, stops=0) == []
    assert len(calls) == 1      # リトライしない


def test_no_results_is_not_retried(provider, monkeypatch):
    calls = []

    def raise_type_error(query, **_kwargs):
        calls.append(query)
        raise TypeError("no results")

    monkeypatch.setattr(ffp, "search_flights", raise_type_error)
    provider.fetch("HND", "SAN", DEP, stops=0)

    # 「便が無い」は待っても増えない。既存実装は3回叩ききってから判定して
    # いたので、直行便の無い路線ごとに無駄なリクエストとバックオフが積み上がった。
    assert len(calls) == 1


# ------------------------------------------------------------------
# (3) リトライとバックオフ
# ------------------------------------------------------------------

def test_transient_failure_is_retried_then_raises_provider_error(provider, monkeypatch):
    calls = []

    def flaky(query, **_kwargs):
        calls.append(query)
        raise RuntimeError("503 Service Unavailable")

    monkeypatch.setattr(ffp, "search_flights", flaky)

    with pytest.raises(ProviderError):
        provider.fetch("HND", "SAN", DEP, stops=0)

    assert len(calls) == 3   # max_retries


def test_retry_recovers_without_raising(provider, monkeypatch):
    attempts = {"n": 0}

    def flaky(_query, **_kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("timeout")
        return [_group(500, ["ANA"], ["SAN"])]

    monkeypatch.setattr(ffp, "search_flights", flaky)

    quotes = provider.fetch("HND", "SAN", DEP, stops=0)
    assert [q.price_usd for q in quotes] == [500.0]


def test_backoff_is_exponential(monkeypatch):
    slept = []
    provider = ffp.FastFlightsProvider(sleep=slept.append)

    def always_fail(_query, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ffp, "search_flights", always_fail)

    with pytest.raises(ProviderError):
        provider.fetch("HND", "SAN", DEP, stops=0)

    # base * 1, base * 2 のバックオフ2回 + 最後のポライトネス待ち1回。
    assert slept[:2] == [3.0, 6.0]
    assert len(slept) == 3


def test_politeness_delay_happens_even_when_the_search_fails(monkeypatch):
    slept = []
    provider = ffp.FastFlightsProvider(sleep=slept.append)

    def raise_type_error(_query, **_kwargs):
        raise TypeError("no results")

    monkeypatch.setattr(ffp, "search_flights", raise_type_error)
    provider.fetch("HND", "SAN", DEP, stops=0)

    # 失敗時こそ連打しない。
    assert len(slept) == 1
    assert 2.0 <= slept[0] <= 5.0


# ------------------------------------------------------------------
# (2) セグメント数での検証
# ------------------------------------------------------------------

def test_itineraries_with_the_wrong_segment_count_are_discarded(provider, monkeypatch):
    # max_stops は「以下」なので stops=1 の検索に直行便が混ざって返る。
    # フィルタを信じると直行便が 1-stop として二重計上される。
    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [
        _group(400, ["ZIPAIR"], ["LAX"]),            # 直行（1セグメント）
        _group(600, ["UA"], ["SFO", "SAN"]),         # 1経由（2セグメント）
    ])

    quotes = provider.fetch("HND", "SAN", DEP, stops=1)

    assert [q.price_usd for q in quotes] == [600.0]
    assert all(q.stops == 1 for q in quotes)


def test_via_airport_is_the_first_segments_arrival(provider, monkeypatch):
    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [
        _group(600, ["UA"], ["SFO", "SAN"]),
    ])

    quotes = provider.fetch("HND", "SAN", DEP, stops=1)
    assert quotes[0].via_airport == "SFO"


def test_nonstop_has_no_via_airport(provider, monkeypatch):
    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [
        _group(700, ["ANA"], ["SAN"]),
    ])

    quotes = provider.fetch("HND", "SAN", DEP, stops=0)
    assert quotes[0].via_airport is None


def test_multiple_airlines_are_joined(provider, monkeypatch):
    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [
        _group(600, ["ANA", "United"], ["SFO", "SAN"]),
    ])

    quotes = provider.fetch("HND", "SAN", DEP, stops=1)
    assert quotes[0].airline == "ANA, United"


# ------------------------------------------------------------------
# LCC 明示検索のためのフィルタ受け渡し
# ------------------------------------------------------------------

def test_airlines_filter_reaches_the_query(provider, monkeypatch):
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return "query"

    monkeypatch.setattr(ffp, "create_query", capture)
    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [])

    provider.fetch("HND", "LAX", DEP, stops=0, airlines=["ZG"])

    assert captured["flights"][0].airlines == ["ZG"]
    assert captured["currency"] == "USD"


def test_no_filter_means_airlines_is_none(provider, monkeypatch):
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return "query"

    monkeypatch.setattr(ffp, "create_query", capture)
    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [])

    provider.fetch("HND", "LAX", DEP, stops=0)

    # 空リストではなく None。空リストを渡すと「どの航空会社も許さない」と
    # 解釈されうる。
    assert captured["flights"][0].airlines is None


# ------------------------------------------------------------------
# 便の詳細（取得済みなのに捨てていた項目）
# ------------------------------------------------------------------

def test_flight_details_are_carried_over(provider, monkeypatch):
    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [
        _group(700, ["ANA"], ["SAN"],
               carbon=SimpleNamespace(emission=812_000, typical_on_route=900_000)),
    ])

    q = provider.fetch("HND", "SAN", DEP, stops=0)[0]

    # 価格以外も持ち帰る。**後から遡って取得できない。**
    assert q.departure_time == "2026-10-15T11:30"
    assert q.arrival_time == "2026-10-15T18:45"
    assert q.air_minutes == 555
    assert q.plane_types == "Boeing 787"
    assert q.carbon_g == 812_000


def test_air_minutes_sums_the_segments(provider, monkeypatch):
    # 乗継時間は含めない。総所要は出発時刻と到着時刻の差から出せる。
    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [
        _group(600, ["UA"], [], segments=[
            _segment("SFO", duration=120, plane="A320"),
            _segment("SAN", duration=90, plane="E175"),
        ]),
    ])

    q = provider.fetch("HND", "SAN", DEP, stops=1)[0]

    assert q.air_minutes == 210
    assert q.plane_types == "A320, E175"


def test_missing_details_do_not_break_collection(provider, monkeypatch):
    # パーサの都合で項目が欠けても、価格の収集は続ける。
    bare = SimpleNamespace(to_airport=SimpleNamespace(code="SAN"))
    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [
        _group(700, ["ANA"], [], segments=[bare]),
    ])

    q = provider.fetch("HND", "SAN", DEP, stops=0)[0]

    assert q.price_usd == 700.0        # 価格は取れている
    assert q.departure_time is None
    assert q.air_minutes is None
    assert q.plane_types is None
    assert q.carbon_g is None


# ------------------------------------------------------------------
# 0件の理由を残す（2026-10-12）。**扱いは変えない。**
#
# 10/1 ごろから、収集の最中にだけ0件が増えた。取得元のエラー（errorHasStatus）
# なのか本当に空のページなのかを、翌朝の記録で見分けるための計測。
# ------------------------------------------------------------------

def test_an_error_status_reply_is_recorded_as_such(provider, monkeypatch):
    """errorHasStatus は parse_flights が FlightsNotFound にして投げる。"""
    from fast_flights.exceptions import FlightsNotFound

    def error_status(*_a, **_k):
        raise FlightsNotFound("no flights found; received error")
    monkeypatch.setattr(ffp, "search_flights", error_status)

    assert provider.fetch("HND", "LAX", DEP, stops=1) == []
    assert provider.last_empty_reason == "error_status"


def test_an_empty_page_is_recorded_as_such(provider, monkeypatch):
    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [])

    assert provider.fetch("HND", "LAX", DEP, stops=1) == []
    assert provider.last_empty_reason == "empty_page"


def test_flights_that_all_fail_the_stop_check_are_recorded_as_filtered(provider, monkeypatch):
    """便は返ったが、経由回数の検証で全部落ちた — 空のページとは別物。"""
    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [
        _group(400, ["ZIPAIR"], ["LAX"]),          # 直行しか無いのに stops=1
    ])

    assert provider.fetch("HND", "LAX", DEP, stops=1) == []
    assert provider.last_empty_reason == "filtered"


def test_the_reason_is_cleared_when_flights_come_back(provider, monkeypatch):
    """**前回の理由を引きずらない。** collector は直後に読むので、残ると取り違える。"""
    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [])
    provider.fetch("HND", "LAX", DEP, stops=1)
    assert provider.last_empty_reason == "empty_page"

    monkeypatch.setattr(ffp, "search_flights", lambda *_a, **_k: [
        _group(600, ["UA"], ["SFO", "LAX"])])
    assert len(provider.fetch("HND", "LAX", DEP, stops=1)) == 1
    assert provider.last_empty_reason is None


def test_recording_the_reason_does_not_change_the_no_retry_behaviour(provider, monkeypatch):
    """**扱いは変えない。** 0件は今までどおり即返す（再試行しない）。"""
    from fast_flights.exceptions import FlightsNotFound
    calls = []

    def error_status(*_a, **_k):
        calls.append(1)
        raise FlightsNotFound("no flights found; received error")
    monkeypatch.setattr(ffp, "search_flights", error_status)

    provider.fetch("HND", "LAX", DEP, stops=1)
    assert len(calls) == 1
