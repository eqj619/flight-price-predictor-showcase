"""health コマンドの分類のテスト。

**この分類が狂うと、警告として使えなくなる。** 直す必要のないものが
「継続中」に並べば無視されるようになり、本当に壊れたときの新規表示も
一緒に無視される。
"""
from datetime import timedelta

import pytest

from app import clock
from app.cli import cmd_health
from app.db import repository


def d(days_ago: int) -> str:
    """clock.today() からの相対日付。

    **固定日付を書かない。** health は「直近 args.days 日」で絞るので、
    固定日付だと日が経つほど範囲外に落ち、ある日から静かに失敗しはじめる。
    """
    return (clock.today() - timedelta(days=days_ago)).isoformat()


def _log(check_date, status="partial", routes=10, records=50):
    repository.insert_collection_log(
        check_date=check_date, status=status, source="test",
        routes_checked=routes, records_saved=records, errors=None, duration_ms=1000)


def _err(check_date, origin, destination, stops=1):
    repository.log_fetch_error(
        check_date=check_date, origin=origin, destination=destination,
        departure_date="2026-12-01", stops=stops, via_airport=None,
        error_message="IndexError: list index out of range")


class _Args:
    days = 14


def _run(capsys):
    code = cmd_health(_Args())
    return code, capsys.readouterr().out


def test_failure_that_stopped_is_not_called_ongoing(capsys):
    """2日前に1度きり失敗し、以後は正常なものを「継続中」に並べない。

    実際に起きた誤判定。SAN->HND は 08-09 に1度失敗しただけで毎日実測が
    取れていたのに「継続中」と表示され、調査対象に見えていた。
    """
    _log(d(2)); _err(d(2), "SAN", "HND", stops=0)
    _log(d(1))
    _log(d(0)); _err(d(0), "LAX", "CST", stops=1)

    code, out = _run(capsys)
    assert "解消した失敗: 1 件" in out
    assert "SAN->HND" in out.split("解消した失敗")[1]
    assert "継続中" not in out
    assert code == 1                      # 当日の新規があるので非ゼロ


def test_ongoing_failure_is_separated_from_new(capsys):
    _log(d(1)); _err(d(1), "NRT", "SYD")
    _log(d(0)); _err(d(0), "NRT", "SYD"); _err(d(0), "HND", "MIL")

    code, out = _run(capsys)
    assert "今日はじめて出た失敗: 1 件" in out
    assert "HND->MIL" in out.split("今日はじめて")[1].split("継続中")[0]
    assert "継続中の失敗: 1 件" in out
    assert "NRT->SYD" in out.split("継続中の失敗")[1]
    assert code == 1


def test_clean_latest_run_reports_everything_resolved(capsys):
    """直近の収集が無事なら、古いエラーが「新規」に化けない。

    基準をエラーの最終日にすると、ここで 08-09 が最新扱いになり
    SAN->HND が新規として警告される。
    """
    _log(d(2)); _err(d(2), "SAN", "HND")
    _log(d(0), status="success")

    code, out = _run(capsys)
    assert "今日はじめて出た失敗はありません" in out
    assert "解消した失敗: 1 件" in out
    assert code == 0                      # 鳴らさない


def test_no_records_at_all(capsys):
    code, out = _run(capsys)
    assert "(記録がありません)" in out
    assert code == 0


def test_days_seen_is_a_count_not_a_streak(capsys):
    """飛び飛びで失敗したものを「連続」と書かない。"""
    _log(d(2)); _err(d(2), "NRT", "SYD")
    _log(d(1))
    _log(d(0)); _err(d(0), "NRT", "SYD")

    _code, out = _run(capsys)
    tail = out.split("継続中の失敗")[1]
    assert "計2日" in tail
    assert "連続" not in tail
