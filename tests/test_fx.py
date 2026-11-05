"""為替（USD/JPY）のテスト。

日本語表示のときだけ使う**表示上の換算**。収集も予測も USD のまま行う。

**為替は補助的な表示であって、価格収集や予測の前提ではない。**
取得に失敗しても何も壊れず、USD 表示に倒れることが最も大事な性質。
"""
import json
from unittest.mock import patch

import pytest

from app import fx
from app.db import repository


def test_no_rate_stored_means_no_conversion():
    """取得できていなければ None。画面は USD のまま出す。"""
    assert fx.current() is None


def test_saved_rate_is_returned():
    repository.save_fx_rate("USD/JPY", "2026-08-13", 159.33)
    assert fx.current() == {"date": "2026-08-13", "rate": 159.33}


def test_the_newest_publication_date_wins():
    repository.save_fx_rate("USD/JPY", "2026-08-10", 158.64)
    repository.save_fx_rate("USD/JPY", "2026-08-13", 159.33)
    repository.save_fx_rate("USD/JPY", "2026-08-11", 158.90)
    assert fx.current()["date"] == "2026-08-13"


def test_a_corrected_rate_overwrites_the_same_day():
    repository.save_fx_rate("USD/JPY", "2026-08-13", 159.33)
    repository.save_fx_rate("USD/JPY", "2026-08-13", 159.40)
    assert fx.current()["rate"] == 159.40


# ----------------------------------------------------------------------
# 取得の失敗
# ----------------------------------------------------------------------

def _response(payload):
    class _R:
        def read(self): return json.dumps(payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return _R()


def test_a_network_failure_returns_none_and_does_not_raise():
    """**日次ジョブから呼ばれるので例外を投げない。**"""
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert fx.fetch() is None


def test_a_broken_payload_returns_none():
    with patch("urllib.request.urlopen", return_value=_response({"oops": 1})):
        assert fx.fetch() is None


@pytest.mark.parametrize("rate", [0.0, 1.5, 900.0, 100000.0])
def test_an_implausible_rate_is_rejected(rate):
    """桁を取り違えた値を通すと「1万円の航空券」を利用者に見せることになる。"""
    payload = {"date": "2026-08-13", "rates": {"JPY": rate}}
    with patch("urllib.request.urlopen", return_value=_response(payload)):
        assert fx.fetch() is None


def test_a_plausible_rate_is_accepted():
    payload = {"date": "2026-08-13", "rates": {"JPY": 159.33}}
    with patch("urllib.request.urlopen", return_value=_response(payload)):
        assert fx.fetch() == {"rate": 159.33, "date": "2026-08-13"}


def test_refresh_stores_what_it_fetched():
    payload = {"date": "2026-08-13", "rates": {"JPY": 159.33}}
    with patch("urllib.request.urlopen", return_value=_response(payload)):
        fx.refresh()
    assert fx.current() == {"date": "2026-08-13", "rate": 159.33}


def test_refresh_keeps_the_last_good_rate_when_the_fetch_fails():
    """取れなかった日に、前の値が消えたり壊れたりしない。"""
    repository.save_fx_rate("USD/JPY", "2026-08-12", 158.90)
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert fx.refresh() is None
    assert fx.current() == {"date": "2026-08-12", "rate": 158.90}


# ----------------------------------------------------------------------
# 日次ジョブと API
# ----------------------------------------------------------------------

def test_the_daily_job_survives_an_fx_outage():
    """為替が落ちても収集と予測は通る。"""
    from datetime import date

    from app.jobs.daily import run_daily
    from tests.test_lifecycle import FakeProvider

    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        summary = run_daily(provider=FakeProvider(), today=date(2026, 8, 13))
    assert "fx" not in summary["failed_steps"]
    assert summary["fx"] is None


def test_the_api_exposes_the_rate():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routing import expand_routes

    repository.save_fx_rate("USD/JPY", "2026-08-13", 159.33)
    client = TestClient(app)
    created = client.post("/api/watches", json={
        "origin": "NRT", "destination": "SAN", "departure_date": "2026-12-01"})
    body = client.get(f"/api/watches/{created.json()['token']}").json()
    assert body["fx"] == {"date": "2026-08-13", "rate": 159.33}


def test_the_api_returns_null_when_there_is_no_rate():
    """レートが無くても API は普通に返る（画面は USD に倒れる）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    created = client.post("/api/watches", json={
        "origin": "NRT", "destination": "SAN", "departure_date": "2026-12-01"})
    assert client.get(f"/api/watches/{created.json()['token']}").json()["fx"] is None


def test_prices_are_still_stored_in_usd():
    """**換算は表示の最後だけ。** 収集も予測も通貨を持たない。"""
    from app.collect.collector import collect_once
    from tests.test_lifecycle import FakeProvider
    from datetime import date

    repository.save_fx_rate("USD/JPY", "2026-08-13", 159.33)
    repository.upsert_monitored_route("NRT", "SAN", "2026-12-01")
    collect_once(FakeProvider(price=700.0), date(2026, 8, 13))

    rows = repository.history_for("NRT", "SAN", "2026-12-01")
    assert rows and all(r["price_usd"] == 700.0 for r in rows)
