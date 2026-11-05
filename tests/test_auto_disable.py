"""取得できないルートの自動停止のテスト。

空港コードの検証を入れる前に登録された不正なルート（NAR / MIL / CST）は、
出発日まで毎日失敗し続け、ルート枠と日次リクエストを食う。実測でも
エラー率が 1.6% → 11.2% に上がり、下げ止まった。

**最も危険なのは、全面障害のときに全ルートを止めてしまうこと。** fast_flights
は非公式スクレイパで、Google 側の変更で全部が同時に壊れうる。そのとき
自動停止が働くと、監視対象を自ら全滅させたうえ「静かに収集ゼロ」になる。
"""
from datetime import date, timedelta

import pytest

from app.config import get_settings
from app.db import repository
from app.jobs.daily import disable_failing_routes


DEPARTURE = (date.today() + timedelta(days=120)).isoformat()


def d(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _route(origin, destination, departure=DEPARTURE):
    return repository.upsert_monitored_route(origin, destination, departure)


def _collected(check_date):
    repository.insert_collection_log(
        check_date=check_date, status="partial", source="test",
        routes_checked=5, records_saved=10, errors=None, duration_ms=1000)


def _failed(check_date, origin, destination, stops=1, departure=DEPARTURE):
    repository.log_fetch_error(
        check_date=check_date, origin=origin, destination=destination,
        departure_date=departure, stops=stops, via_airport=None,
        error_message="IndexError: list index out of range")


def _got_data(check_date, origin, destination, departure=DEPARTURE):
    repository.upsert_snapshots([repository.Snapshot(
        check_date=check_date, departure_date=departure, origin=origin,
        destination=destination, stops=0, via_airport=None, airline="ANA",
        price_usd=800.0, source="real", fetched_at=f"{check_date}T03:00:00")])


def _three_days():
    for i in (2, 1, 0):
        _collected(d(i))


def test_a_route_that_never_returns_anything_is_disabled():
    _route("FUK", "NAR")
    _three_days()
    for i in (2, 1, 0):
        _failed(d(i), "FUK", "NAR")
        _got_data(d(i), "NRT", "SAN")      # 他は健全（全面障害ではない）
    _route("NRT", "SAN")

    result = disable_failing_routes()
    assert result["disabled"] == 1
    disabled = repository.disabled_routes()
    assert [(r["origin"], r["destination"]) for r in disabled] == [("FUK", "NAR")]
    assert "3回連続" in disabled[0]["disabled_reason"]


def test_a_route_that_partly_works_is_kept():
    """経由回数ごとに検索するので、stops=1 が落ちても stops=0 が取れる。

    実際 NRT-SYD がこの形。価値を出しているので止めてはいけない。
    """
    _route("NRT", "SYD")
    _three_days()
    for i in (2, 1, 0):
        _failed(d(i), "NRT", "SYD", stops=1)
        _got_data(d(i), "NRT", "SYD")      # stops=0 は取れている

    assert disable_failing_routes()["disabled"] == 0
    assert repository.disabled_routes() == []


def test_a_route_that_failed_only_some_days_is_kept():
    _route("SAN", "HND")
    _three_days()
    _failed(d(2), "SAN", "HND")            # 3日のうち1日だけ
    for i in (2, 1, 0):
        _got_data(d(i), "NRT", "SAN")
    _route("NRT", "SAN")

    assert disable_failing_routes()["disabled"] == 0


# ----------------------------------------------------------------------
# 全面障害の歯止め
# ----------------------------------------------------------------------

def test_widespread_failure_disables_nothing():
    """**閾値を超えたら1本も止めない。** 個別の不良ではなく障害だから。"""
    routes = [("NRT", "SAN"), ("HND", "LAX"), ("NRT", "SFO"),
              ("HND", "SEA"), ("LAX", "NRT")]
    for o, dst in routes:
        _route(o, dst)
    _three_days()
    for i in (2, 1, 0):
        for o, dst in routes:
            _failed(d(i), o, dst)

    result = disable_failing_routes()
    assert result["disabled"] == 0
    assert result["candidates"] == len(routes)
    assert result["skipped"] == "全面障害の疑い"
    assert repository.disabled_routes() == []


def test_isolated_failure_below_the_threshold_is_disabled():
    """歯止めの境目。健全なルートが十分あれば、不良だけを止める。"""
    for i in range(9):
        _route("AAA", f"B{i:02d}")
    _route("FUK", "NAR")
    _three_days()
    for i in (2, 1, 0):
        _failed(d(i), "FUK", "NAR")
        for n in range(9):
            _got_data(d(i), "AAA", f"B{n:02d}")

    result = disable_failing_routes()
    assert result["candidates"] == 1
    assert result["disabled"] == 1          # 1/10 = 10% < 30%


# ----------------------------------------------------------------------
# 判断材料が足りないとき
# ----------------------------------------------------------------------

def test_nothing_happens_before_enough_collections():
    """収集の実績が足りないうちは止めない。"""
    _route("FUK", "NAR")
    _collected(d(1))
    _failed(d(1), "FUK", "NAR")

    result = disable_failing_routes()
    assert result["disabled"] == 0
    assert result["skipped"] == "収集日数が不足"


def test_days_without_a_collection_do_not_count_as_failures():
    """スケジューラが止まっていた日を「失敗した日」に数えない。

    こちらの都合でルートを止めることになる。
    """
    _route("FUK", "NAR")
    # 収集が走ったのは 10日前・5日前・今日の3回だけ
    for i in (10, 5, 0):
        _collected(d(i))
        _failed(d(i), "FUK", "NAR")
    _route("NRT", "SAN")
    for i in (10, 5, 0):
        _got_data(d(i), "NRT", "SAN")

    # 間の日は収集していないので、3回すべてで失敗＝停止の条件を満たす
    assert disable_failing_routes()["disabled"] == 1


def test_a_departed_route_is_left_to_the_other_cleanup():
    past = (date.today() - timedelta(days=1)).isoformat()
    repository.upsert_monitored_route("FUK", "NAR", past)
    _three_days()
    for i in (2, 1, 0):
        _failed(d(i), "FUK", "NAR", departure=past)

    assert disable_failing_routes()["disabled"] == 0


# ----------------------------------------------------------------------
# 復活
# ----------------------------------------------------------------------

def test_re_registering_revives_the_route_and_clears_the_record():
    """利用者が改めて登録したら復活させる。

    状況が直っていなければ数日後にまた止まるだけで、無駄は数日分に収まる。
    永久に死んだままにするより、直ったときに戻れるほうがよい。
    """
    _route("FUK", "NAR")
    _three_days()
    for i in (2, 1, 0):
        _failed(d(i), "FUK", "NAR")
        _got_data(d(i), "NRT", "SAN")
    _route("NRT", "SAN")
    assert disable_failing_routes()["disabled"] == 1

    _route("FUK", "NAR")                    # 再登録
    assert repository.disabled_routes() == []
    assert any(r.origin == "FUK" for r in repository.active_routes())


def test_the_route_row_is_kept_so_history_survives():
    """止めても行は消さない。価格履歴と first_seen_at を残す。"""
    _route("FUK", "NAR")
    _three_days()
    for i in (2, 1, 0):
        _failed(d(i), "FUK", "NAR")
        _got_data(d(i), "NRT", "SAN")
    _route("NRT", "SAN")
    disable_failing_routes()

    with repository.get_connection() as conn:
        row = conn.execute(
            "SELECT active, disabled_at, disabled_reason FROM monitored_routes "
            "WHERE origin = 'FUK' AND destination = 'NAR'").fetchone()
    assert row["active"] == 0
    assert row["disabled_at"] and row["disabled_reason"]


# ----------------------------------------------------------------------
# 利用者への伝達
# ----------------------------------------------------------------------

def test_the_api_says_why_a_leg_stopped_collecting():
    """**黙って空にしない。** データが来ない理由が画面に届くこと。

    止めた区間は他の区間と見分けがつかず、利用者には「壊れている」と
    しか見えない。
    """
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    created = client.post("/api/watches", json={
        "origin": "NRT", "destination": "SYD", "departure_date": DEPARTURE})
    assert created.status_code == 201, created.text
    token = created.json()["token"]

    _three_days()
    for i in (2, 1, 0):
        _failed(d(i), "NRT", "SYD")
        _got_data(d(i), "HND", "SYD")     # 近接展開のもう片方は健全
    # 全面障害と見なされない規模にするため、健全なルートを並べる
    for n in range(9):
        _route("AAA", f"B{n:02d}")
        for i in (2, 1, 0):
            _got_data(d(i), "AAA", f"B{n:02d}")

    assert disable_failing_routes()["disabled"] == 1

    body = client.get(f"/api/watches/{token}").json()
    stopped = [r for r in body["routes"] if r["active"] is False]
    assert len(stopped) == 1
    assert (stopped[0]["origin"], stopped[0]["destination"]) == ("NRT", "SYD")
    assert "取得できず" in stopped[0]["disabled_reason"]
    # 生きている区間には理由が付かない
    alive = [r for r in body["routes"] if r["active"]]
    assert all(r["disabled_reason"] is None for r in alive)


# ----------------------------------------------------------------------
# ルート枠の監視
# ----------------------------------------------------------------------

def test_capacity_is_reported_and_warns_before_the_cap(monkeypatch, caplog):
    """**上限に達してから気づくと、伸びている最中に受付を止めることになる。**

    2026-08-14 に利用者由来の増加が 47本/日 に達し残り6日まで詰まったが、
    気づいたのは手で KPI を眺めたからだった。
    """
    import logging

    from app.config import get_settings
    from app.jobs.daily import check_route_capacity

    monkeypatch.setenv("MAX_ACTIVE_ROUTES", "10")
    get_settings.cache_clear()

    for i in range(7):                       # 70% — まだ警告しない
        _route("AAA", f"B{i:02d}")
    assert check_route_capacity()["level"] == "ok"

    with caplog.at_level(logging.ERROR):
        _route("AAA", "B07")                 # 80% — 警告
        state = check_route_capacity()
    assert state["level"] == "warn"
    assert state["active"] == 8 and state["capacity"] == 10
    assert "逼迫" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.ERROR):
        for i in range(8, 10):
            _route("AAA", f"B{i:02d}")       # 100%
        assert check_route_capacity()["level"] == "full"
    assert "上限に達した" in caplog.text


def test_capacity_check_does_not_disable_anything():
    """判断は人がする。ログに出すだけで、勝手に止めない。"""
    from app.jobs.daily import check_route_capacity

    _route("AAA", "B01")
    check_route_capacity()
    assert repository.count_active_routes() == 1
    assert repository.disabled_routes() == []


# --- 「便が無い」を「取れない」と区別する（2026-08-30）---------------
#
# **利用者の報告。** NRT-GMP（運航されていない区間）を登録すると、
# モデル推定の価格が延々と表示され続けた。運航されていない区間は例外を
# 出さず0件が返るだけなので fetch_errors に何も残らず、自動停止の判定に
# かからなかった。**永久に止まらないまま存在しない便の値段を出していた。**

def _empty(check_date, origin, destination, stops=1, departure=DEPARTURE):
    """検索は通ったが便が0件だった、を記録する。"""
    repository.log_empty_search(
        check_date=check_date, origin=origin, destination=destination,
        departure_date=departure, stops=stops)


def _five_days():
    for i in (4, 3, 2, 1, 0):
        _collected(d(i))


def test_a_route_with_no_flights_at_all_is_disabled():
    """**エラーが1件も無くても止める。** これが 2026-08-30 に塞いだ穴。"""
    _route("NRT", "GMP")
    _five_days()
    for i in (4, 3, 2, 1, 0):
        _empty(d(i), "NRT", "GMP", stops=0)
        _empty(d(i), "NRT", "GMP", stops=1)

    assert disable_failing_routes()["disabled"] == 1
    stopped = repository.disabled_routes()
    assert [(r["origin"], r["destination"]) for r in stopped] == [("NRT", "GMP")]
    assert "便が0件" in stopped[0]["disabled_reason"]


def test_no_flights_and_fetch_failure_are_told_apart():
    """**利用者が次に取る行動が違う。** 待つのか、別の空港を試すのか。"""
    _route("NRT", "GMP")        # 便が無い
    _route("FUK", "NAR")        # 取得に失敗する
    _five_days()
    for i in (4, 3, 2, 1, 0):
        _empty(d(i), "NRT", "GMP")
    for i in (2, 1, 0):
        _failed(d(i), "FUK", "NAR")

    disable_failing_routes()
    kinds = {(r["origin"], r["destination"]): r["disabled_kind"]
             for r in repository.disabled_routes()}
    assert kinds[("NRT", "GMP")] == "no_flights"
    assert kinds[("FUK", "NAR")] == "fetch_failed"


def test_a_brand_new_route_is_not_disabled_for_having_no_data():
    """**試した証拠が無いルートを止めない。**

    実測ゼロだけを条件にすると、昨日登録されたばかりのルートを
    「何も取れていない」と見なして即座に止めてしまう。
    """
    _route("NRT", "GMP")
    _three_days()
    _empty(d(0), "NRT", "GMP")      # 今日1回だけ試した

    assert disable_failing_routes()["disabled"] == 0


def test_a_route_that_returns_something_is_never_disabled():
    """0件の日があっても、1日でも取れていれば止めない。"""
    _route("NRT", "LAX")
    _three_days()
    for i in (2, 1, 0):
        _empty(d(i), "NRT", "LAX", stops=1)
    _got_data(d(1), "NRT", "LAX")

    assert disable_failing_routes()["disabled"] == 0


# --- 上流の不調を「路線が無い」と読まないための歯止め（2026-09-01）------
#
# 0件を停止の根拠に加えたことで、上流が不調な日を「運航されていない」と
# 読む危険を作ってしまった。実際 2026-09-01 に FUK-HND・HND-OKA・LAX-NRT
# など**明らかに実在する路線**が17本まとめて0件を返した。あと1日続けば
# 停止されていた（全面障害の歯止めは 17/447 = 3.8% で発動しない）。

def test_a_route_that_once_returned_prices_is_never_called_no_flights():
    """**一度でも実価格が返ったなら、その区間に便はある。**

    その後0件が続くのは上流の問題であって、路線が無いのではない。
    2026-09-01 の FUK-HND がこれ。
    """
    _route("FUK", "HND")
    _got_data(d(9), "FUK", "HND")          # 以前は取れていた
    _collected(d(9))
    _five_days()
    for i in (4, 3, 2, 1, 0):
        _empty(d(i), "FUK", "HND")

    disable_failing_routes()
    stopped = {(r["origin"], r["destination"]): r["disabled_kind"]
               for r in repository.disabled_routes()}
    assert stopped.get(("FUK", "HND")) != "no_flights", \
        "取れていた路線を「便が無い」と判定してはいけない"


def test_three_empty_days_are_not_enough_to_call_it_no_flights():
    """0件はエラーより弱い証拠。**急ぐ理由が無いので日数で吸収する。**"""
    _route("NRT", "GMP")
    _three_days()
    for i in (2, 1, 0):
        _empty(d(i), "NRT", "GMP")

    assert disable_failing_routes()["disabled"] == 0


def test_a_widespread_empty_day_does_not_disable_real_routes():
    """2026-09-01 の再現。実在する路線が同時に0件を返した日。

    **全面障害の歯止めには頼れない。** あの日は 17/447 = 3.8% で、
    30% の閾値に遠く届かなかった。ここでも健全なルートを多めに置いて
    割合を下げ、**歯止めが発動しない状況**で守れることを確かめる。
    """
    legs = [("FUK", "HND"), ("HND", "OKA"), ("LAX", "NRT"), ("SAN", "HND")]
    for o, dst in legs:
        _route(o, dst)
        _got_data(d(9), o, dst)            # どれも以前は取れていた
    # 正常に取れているルートを 20 本。候補の割合を 4/24 = 17% に落とす。
    for n in range(20):
        _route("AAA", f"B{n:02d}")
    _collected(d(9))
    _five_days()
    for i in (4, 3, 2, 1, 0):
        for o, dst in legs:
            _empty(d(i), o, dst)
        for n in range(20):
            _got_data(d(i), "AAA", f"B{n:02d}")

    disable_failing_routes()
    kinds = {(r["origin"], r["destination"]): r["disabled_kind"]
             for r in repository.disabled_routes()}
    for leg in legs:
        assert kinds.get(leg) != "no_flights", f"{leg} を運航なしと判定した"


# ── 予約受付前の出発日を「運航されていない」と判定しない ────────────
#
# **2026-10-11 の事故。** SAN-NRT の出発日1年分を 10/6 に調査用として登録した
# ところ、2027-09-05 以降の57本が5日連続0件で「運航されていない」と止められた。
# 航空会社は出発の約330日前まで予約を受け付けないので、それより先は検索しても
# 便が出てこないのが正常。登録したばかりで一度も取れていないので、「一度でも
# 取れたら止めない」歯止めも効かなかった。

def _days_ahead(n):
    """今日から n 日後の出発日。"""
    return (date.today() + timedelta(days=n)).isoformat()


def _empty_five_days(origin, destination, departure):
    _route(origin, destination, departure)
    _five_days()
    for i in (4, 3, 2, 1, 0):
        _empty(d(i), origin, destination, stops=0, departure=departure)
        _empty(d(i), origin, destination, stops=1, departure=departure)


def test_a_departure_beyond_the_booking_window_is_not_called_no_flights():
    """**これが 10/11 に起きたこと。** 予約受付前は0件が正常。"""
    horizon = get_settings().booking_horizon_days
    _empty_five_days("SAN", "NRT", _days_ahead(horizon + 60))

    assert disable_failing_routes()["disabled"] == 0
    assert repository.disabled_routes() == []


def test_a_route_that_just_entered_the_booking_window_is_not_stopped():
    """**窓のいちばん古い日で測る。** 今日受付に入っても、窓の中に受付前の
    空振りが混ざっていれば証拠にしない（2027-09-03/04 がこの境目にいた）。"""
    horizon = get_settings().booking_horizon_days
    # 今日は受付の内側（horizon - 2 日先）だが、4日前には外側だった。
    _empty_five_days("SAN", "NRT", _days_ahead(horizon - 2))

    assert disable_failing_routes()["disabled"] == 0


def test_inside_the_window_for_all_five_days_is_still_stopped():
    """**本来の役目は残す。** 5日とも受付中で0件なら、運航されていない。

    境目ちょうど（窓の最古の日から horizon 日先）は判定の対象に入る。
    """
    horizon = get_settings().booking_horizon_days
    # 窓の最古の日は4日前。そこから数えてちょうど horizon 日先。
    _empty_five_days("NRT", "GMP", _days_ahead(horizon - 4))

    assert disable_failing_routes()["disabled"] == 1
    assert repository.disabled_routes()[0]["disabled_kind"] == "no_flights"


def test_the_reason_states_the_window_actually_used():
    """**判定に使った日数と期間を書く。** 2026-10-11 まで、5日で判定した停止を
    「3回連続」と記録していた（取得失敗用の3日の窓で文言を作っていたため）。"""
    _empty_five_days("NRT", "GMP", DEPARTURE)
    assert disable_failing_routes()["disabled"] == 1

    reason = repository.disabled_routes()[0]["disabled_reason"]
    days = get_settings().auto_disable_after_empty_days
    assert f"{days}回連続で便が0件" in reason
    assert f"（{d(days - 1)}〜{d(0)}）" in reason
