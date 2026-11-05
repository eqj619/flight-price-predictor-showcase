"""取得元が持つ過去価格の抽出と保存。

**この機能は補助的で、壊れても日次収集を止めてはいけない。** テストの重心は
「正しく取れること」より「取れないときに静かに諦めること」に置く。抽出は
fast_flights が読んでいない添字に依存していて、Google 側の都合でいつ形が
変わってもおかしくないため。
"""
import json
from datetime import date

import pytest

from app.collect.fast_flights_provider import parse_history
from app.collect.provider import HistoryPoint, PriceProvider
from app.db import repository


def build_html(series) -> str:
    """`script.ds:1` に本物と同じ形のペイロードを埋めた HTML を作る。

    実応答は 1MB 超あるので、抽出が見ている構造だけを最小限で再現する。
    payload[5][10][0] に時系列、それ以外は本物同様に埋まっていない。
    """
    payload = [None] * 8
    payload[3] = [None]
    payload[5] = [4, None, None, None, None, None, 1, None, None, None, [series]]
    return f'<script class="ds:1">data: {json.dumps(payload)}, sideChannel: {{}}</script>'


MILLIS = {  # UTC 正午。日付境界の取り違えを検出できるよう昼にしてある
    "2026-06-15": 1781524800000,
    "2026-06-16": 1781611200000,
    "2026-06-17": 1781697600000,
}


# ----------------------------------------------------------------------
# 抽出
# ----------------------------------------------------------------------

def test_series_is_parsed_in_date_order():
    html = build_html([[MILLIS["2026-06-17"], 470],
                       [MILLIS["2026-06-15"], 455],
                       [MILLIS["2026-06-16"], 454]])
    points = parse_history(html)

    assert [p.observed_date for p in points] == [
        date(2026, 6, 15), date(2026, 6, 16), date(2026, 6, 17)]
    assert [p.price_usd for p in points] == [455.0, 454.0, 470.0]


@pytest.mark.parametrize("html", [
    "",
    "<html></html>",
    '<script class="ds:1">data: {壊れた JSON</script>',
    '<script class="ds:1">data: [], sideChannel: {}</script>',        # 添字が無い
    '<script class="ds:1">data: [1,2,3,4,5,null], sideChannel: {}</script>',  # [5] が None
])
def test_broken_input_yields_empty_not_exception(html):
    """**例外を投げない。** 呼び出し側は「取れたら得」として扱う。"""
    assert parse_history(html) == []


def test_series_present_but_empty():
    assert parse_history(build_html([])) == []


@pytest.mark.parametrize("bad", [
    [None, 455],              # 時刻が無い
    [MILLIS["2026-06-15"], None],   # 価格が無い
    ["not-a-number", 455],
    [MILLIS["2026-06-15"], 0],      # 桁違いに小さい
    [MILLIS["2026-06-15"], 999_999],  # 桁違いに大きい
    [10 ** 20, 455],          # 表現できない日付
])
def test_bad_points_are_skipped_without_losing_the_rest(bad):
    """**1点の異常で系列全体を捨てない。** 使える点は残す。"""
    html = build_html([[MILLIS["2026-06-15"], 455], bad,
                       [MILLIS["2026-06-17"], 470]])
    points = parse_history(html)

    assert [p.price_usd for p in points] == [455.0, 470.0]


def test_duplicate_dates_keep_the_first():
    """同じ日が2回来ても1点にする。UNIQUE 制約にぶつける前に潰す。"""
    html = build_html([[MILLIS["2026-06-15"], 455],
                       [MILLIS["2026-06-15"], 999]])
    points = parse_history(html)

    assert len(points) == 1
    assert points[0].price_usd == 455.0


# ----------------------------------------------------------------------
# 抽象の既定動作
# ----------------------------------------------------------------------

def test_provider_without_history_support_returns_empty():
    """**fetch_history は抽象メソッドではない。** 公式 API へ差し替えたときに
    空実装を書かされないよう、既定で「未対応」を返す。"""
    class Minimal(PriceProvider):
        def fetch(self, origin, destination, departure_date, stops, airlines=None,
              return_date=None):
            return []

    assert Minimal().fetch_history("NRT", "LAX", date(2026, 10, 12)) == []


# ----------------------------------------------------------------------
# 保存
# ----------------------------------------------------------------------

def test_save_and_read_round_trip():
    n = repository.save_price_history("NRT", "LAX", "2026-10-12",
                                      [("2026-06-15", 455.0), ("2026-06-16", 454.0)])
    assert n == 2

    rows = repository.price_history("NRT", "LAX", "2026-10-12")
    assert rows == [{"observed_date": "2026-06-15", "price_usd": 455.0},
                    {"observed_date": "2026-06-16", "price_usd": 454.0}]


def test_recapturing_the_same_day_overwrites():
    """取得元が過去分を訂正することがある。新しい写しを正とする。"""
    repository.save_price_history("NRT", "LAX", "2026-10-12", [("2026-06-15", 455.0)])
    repository.save_price_history("NRT", "LAX", "2026-10-12", [("2026-06-15", 470.0)])

    rows = repository.price_history("NRT", "LAX", "2026-10-12")
    assert len(rows) == 1
    assert rows[0]["price_usd"] == 470.0


def test_saving_nothing_is_not_an_error():
    assert repository.save_price_history("NRT", "LAX", "2026-10-12", []) == 0


def test_history_is_kept_separate_per_route():
    repository.save_price_history("NRT", "LAX", "2026-10-12", [("2026-06-15", 455.0)])
    repository.save_price_history("NRT", "LAX", "2026-10-13", [("2026-06-15", 999.0)])
    repository.save_price_history("HND", "LAX", "2026-10-12", [("2026-06-15", 111.0)])

    assert repository.price_history("NRT", "LAX", "2026-10-12")[0]["price_usd"] == 455.0
    assert repository.price_history("NRT", "LAX", "2026-10-13")[0]["price_usd"] == 999.0
    assert repository.price_history("HND", "LAX", "2026-10-12")[0]["price_usd"] == 111.0


# ----------------------------------------------------------------------
# 取り込み対象の選び方
# ----------------------------------------------------------------------

def add_route(origin, destination, departure_date, active=1):
    from app.db.connection import get_connection
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO monitored_routes (origin, destination, departure_date, "
            "active, first_seen_at) VALUES (?, ?, ?, ?, datetime('now'))",
            (origin, destination, departure_date, active))


def future(days: int) -> str:
    from datetime import timedelta
    return (date.today() + timedelta(days=days)).isoformat()


def test_pending_excludes_routes_already_captured():
    """**遡り取り込みは1ルート1回。** 翌日以降の1点は snapshots で観測している。"""
    add_route("NRT", "LAX", future(60))
    add_route("HND", "LAX", future(60))
    repository.save_price_history("NRT", "LAX", future(60), [("2026-06-15", 455.0)])

    pending = repository.routes_without_history()

    assert [(r["origin"], r["destination"]) for r in pending] == [("HND", "LAX")]


def test_pending_excludes_inactive_and_past_routes():
    add_route("NRT", "LAX", future(60))
    add_route("HND", "LAX", future(60), active=0)
    add_route("SFO", "NRT", future(-5))     # 出発済み

    pending = repository.routes_without_history()

    assert [(r["origin"], r["destination"]) for r in pending] == [("NRT", "LAX")]


def test_summary_reports_coverage():
    repository.save_price_history("NRT", "LAX", "2026-10-12",
                                  [("2026-06-15", 455.0), ("2026-06-16", 454.0)])
    repository.save_price_history("HND", "LAX", "2026-10-12", [("2026-07-01", 700.0)])

    s = repository.price_history_summary()

    assert s == {"points": 3, "routes": 2,
                 "since": "2026-06-15", "until": "2026-07-01"}


def test_summary_on_empty_database():
    """1件も無い状態で落ちない（CLI が起動直後に呼ぶ）。"""
    s = repository.price_history_summary()

    assert s["points"] == 0
    assert s["since"] is None


# --- 便の解析（2026-09-02）------------------------------------------
#
# **1便の欠損で区間ごと捨てない。** ライブラリの parse_js は
# `price = k[1][0][1]` を無防備に読み、価格の無い便が1件でも混ざると
# IndexError で落ちる。2026-09-01 に日本国内線と近距離アジアが一斉に
# これで落ち、13ルートの価格が2日間まるごと失われた。

from app.collect.fast_flights_provider import parse_flights


def _segment(dep_code="HND", arr_code="KMJ"):
    """応答の1区間ぶん。位置は実測で突き止めたもの。"""
    seg = [None] * 23
    seg[3], seg[4] = dep_code, f"{dep_code} Airport"
    seg[6], seg[5] = arr_code, f"{arr_code} Airport"
    seg[8], seg[20] = [10, 30], [2026, 10, 14]      # 出発 時刻 / 日付
    seg[10], seg[21] = [12, 5], [2026, 10, 14]      # 到着
    seg[11] = 95                                     # 飛行時間（分）
    seg[17] = "Boeing 737"
    return seg


def _itinerary(price, airlines=("ANA",), segments=None):
    flight = [None] * 23
    flight[0] = "one-way"
    flight[1] = list(airlines)
    flight[2] = segments if segments is not None else [_segment()]
    extras = [None] * 9
    extras[7], extras[8] = 120_000, 130_000          # 排出量
    flight[22] = extras
    # 価格が無い便は k[1][0] が空で来る。それを再現する。
    money = [[None, price]] if price is not None else [[]]
    return [flight, money]


def _html(payload):
    return ('<html><body><script class="ds:1">AF_initDataCallback({data:'
            + json.dumps(payload) + ', sideChannel: {}});</script></body></html>')


def _payload(itineraries, other=None):
    """応答の骨。**便のリストは2ブロックある。**

    `itineraries` が [3][0]、`other` が [2][0]。2026-09-08 の実測では
    どちらにも同じ検索の便が入っており、安い便は片方に偏っていなかった。
    """
    p = [None] * 8
    p[3] = [itineraries]
    if other is not None:
        p[2] = [other]
    return p


def test_a_flight_without_a_price_does_not_discard_the_others():
    """**これが 2026-09-01 に起きたこと。** 1件の欠損で全部消えていた。"""
    html = _html(_payload([_itinerary(140.0), _itinerary(None), _itinerary(88.0)]))
    got = parse_flights(html)
    assert [f.price for f in got] == [140.0, 88.0]


def test_a_priced_flight_carries_its_details():
    got = parse_flights(_html(_payload([_itinerary(140.0, airlines=("ANA", "JAL"))])))
    assert len(got) == 1
    f = got[0]
    assert f.price == 140.0
    assert f.airlines == ["ANA", "JAL"]
    assert f.flights[0].from_airport.code == "HND"
    assert f.flights[0].to_airport.code == "KMJ"
    assert f.flights[0].duration == 95
    assert f.carbon.emission == 120_000


def test_an_absurd_price_is_dropped():
    """桁違いの値を黙って取り込まない（parse_history と同じ構え）。"""
    got = parse_flights(_html(_payload([_itinerary(3.0), _itinerary(500_000.0),
                                        _itinerary(140.0)])))
    assert [f.price for f in got] == [140.0]


def test_no_flights_is_an_empty_list_not_an_error():
    p = [None] * 8
    p[3] = [None]
    assert parse_flights(_html(p)) == []


def test_a_broken_response_raises_instead_of_looking_empty():
    """**「解析できて0件」と「解析できない」を混ぜない。**

    混ぜると、解析が全面的に壊れた日に「どの区間にも便が無い」と見え、
    誤り件数も collection_status も健全なまま警報が鳴らない。
    """
    with pytest.raises(Exception):
        parse_flights("<html><body>no script here</body></html>")


def test_an_unreadable_itinerary_is_skipped_not_fatal():
    broken = [["not", "a", "flight"], [[None, 140.0]]]
    got = parse_flights(_html(_payload([broken, _itinerary(88.0)])))
    assert [f.price for f in got] == [88.0]


# ── 2つ目のブロック ────────────────────────────────────────────
#
# **2026-09-08 に見つけた取りこぼし。** 応答には同じ検索の便が2ブロックに
# 分かれて入っているのに、[3][0] しか読んでいなかった。毎日およそ半分の便を
# 捨てていたことになる。便がどちらに入るかは日によって変わるため、同じ便が
# 現れたり消えたりし、SAN→HND の価格の線は31日中10日が空いた。


def test_the_second_block_is_read_too():
    """**これが直したこと。** 片方しか読まないと便が半分になる。"""
    got = parse_flights(_html(_payload(
        [_itinerary(140.0, airlines=("ANA",))],
        other=[_itinerary(88.0, airlines=("JAL",)),
               _itinerary(210.0, airlines=("Delta",))])))
    assert sorted(f.price for f in got) == [88.0, 140.0, 210.0]


def test_the_second_block_may_simply_be_absent():
    """入っていない検索もある。**それは異常ではない。**"""
    got = parse_flights(_html(_payload([_itinerary(140.0)])))
    assert [f.price for f in got] == [140.0]


@pytest.mark.parametrize("junk", [None, 0, "x", {"a": 1}])
def test_a_malformed_second_block_does_not_lose_the_first(junk):
    """**後ろのブロックは足すだけ。** 形が違っても前を巻き添えにしない。

    ここで例外に倒すと、2026-09-01 と同じく区間ごと価格を失う。
    """
    p = _payload([_itinerary(140.0)])
    p[2] = [junk] if junk is not None else None
    assert [f.price for f in parse_flights(_html(p))] == [140.0]


def test_no_flights_stays_no_flights_even_with_a_second_block():
    """**「便が無い」を名乗れるのは最初のブロックだけ。**

    ここが None のとき、取得元は「条件に合う便は無い」と言っている。
    2つ目を足しに行くと、その返事を自分で塗り潰すことになる。
    """
    p = [None] * 8
    p[3] = [None]
    p[2] = [[_itinerary(140.0)]]
    assert parse_flights(_html(p)) == []


def test_the_same_flight_in_both_blocks_is_counted_once():
    """重なっても2度数えない。**2度数えると平均が静かに歪む。**"""
    same = _itinerary(140.0, airlines=("ANA",))
    got = parse_flights(_html(_payload([same], other=[json.loads(json.dumps(same))])))
    assert [f.price for f in got] == [140.0]


def test_two_different_flights_at_the_same_price_both_survive():
    """**値段が同じでも別便は別便。** 重複除去で消してはいけない。"""
    late = _segment()
    late[8], late[10] = [17, 0], [19, 40]        # 出発・到着だけ違う
    got = parse_flights(_html(_payload(
        [_itinerary(140.0)], other=[_itinerary(140.0, segments=[late])])))
    assert len(got) == 2


# ── 省かれた時刻の成分 ────────────────────────────────────────
#
# **2026-09-08 に見つけた3つ目の欠陥。** 取得元は時刻の 0 の成分を落とす。
# `hh, mm = stamp.time` と決め打っていたため、正時ちょうどの便（[17]）と
# 深夜0時台の便（[None, 50]）は時刻をまるごと失っていた。実測18便のうち6便。
#
# 時刻を失うと総所要も出せない。総所要が無い行は「不明」として24時間の
# 上限をすり抜けるので、**長すぎる便ばかりが生き残る**偏りまで生んでいた。

from app.collect.fast_flights_provider import _hhmm, _iso_local


class _Stamp:
    def __init__(self, date, time):
        self.date, self.time = date, time


@pytest.mark.parametrize("parts,expected", [
    ([6, 15], (6, 15)),
    ([17], (17, 0)),            # 分が 0 なので落ちている
    ([None, 50], (0, 50)),      # 時が 0 なので None
    ([23], (23, 0)),
    ([None, 5], (0, 5)),
    ([0, 0], (0, 0)),
    ([23, 59], (23, 59)),
])
def test_an_omitted_zero_is_read_as_zero(parts, expected):
    assert _hhmm(parts) == expected


@pytest.mark.parametrize("parts", [
    [], None, "17", [24, 0], [12, 60], [-1, 0], [1, 2, 3], ["a", "b"],
])
def test_a_time_we_do_not_recognise_is_unknown_not_guessed(parts):
    """**推測しない。** 知らない形に 0 を当てはめると嘘の時刻が入る。"""
    assert _hhmm(parts) is None


def test_a_flight_on_the_hour_keeps_its_departure_time():
    """これを落としていた。**正時の便は珍しくない。**"""
    assert _iso_local(_Stamp([2026, 10, 8], [17])) == "2026-10-08T17:00"
    assert _iso_local(_Stamp([2026, 10, 8], [None, 50])) == "2026-10-08T00:50"


def test_a_flight_on_the_hour_still_gets_its_duration():
    """時刻を失うと総所要も出せない。**そこが24時間の上限の抜け穴だった。**"""
    first, second = _segment("SAN", "LAX"), _segment("LAX", "HND")
    first[20] = first[21] = [2026, 10, 8]
    first[8], first[10] = [17], [18, 3]         # 17:00 発
    first[11] = 63
    second[8], second[10] = [None, 50], [5]     # 00:50 発 → 05:00 着
    second[20] = second[21] = [2026, 10, 9]
    second[11] = 730
    got = parse_flights(_html(_payload(
        [_itinerary(676.0, airlines=("United", "ANA"),
                    segments=[first, second])])))
    assert len(got) == 1
    from app.collect.fast_flights_provider import _flight_details
    details = _flight_details(got[0])
    assert details["departure_time"] == "2026-10-08T17:00"
    assert details["total_minutes"] is not None


# ── 「該当便なし」と「読めない」 ──────────────────────────────
#
# **2026-09-08 に見つけた4つ目の欠陥。** 直行便の無い区間を直行便のみで
# 検索すると payload[2..5] が全て None になる。ここを「読めない」に倒して
# いたため、**直行便の無い区間は毎日 fetch_errors に1件積み、しかも
# 「便が0件だった」証拠が残らなかった**。運航されていない区間を見つける
# 仕組みが、この経路では働いていなかった。


def test_a_page_with_no_results_is_zero_flights_not_an_error():
    """SAN→HND を直行便のみで検索したときの形（2026-09-08 実測）。"""
    p = [None] * 32
    p[0], p[1] = [1, 2, 3, 4, 5], [1]      # メタ情報は届いている
    assert parse_flights(_html(p)) == []


def test_a_payload_too_short_to_hold_flights_still_raises():
    """**「読めない」を「便が無い」に混ぜない。**

    混ぜると、解析が全面的に壊れた日に「どの区間にも便が無い」と見え、
    誤り件数も collection_status も健全なまま警報が鳴らない。
    """
    with pytest.raises(Exception):
        parse_flights(_html([1, 2]))
    with pytest.raises(Exception):
        parse_flights(_html({"not": "a list"}))
