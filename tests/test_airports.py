"""空港マスタと登録時の検証のテスト。

きっかけは実際の失敗。形式（3文字の英字）しか見ていなかったため、
NAR（NRT の打ち間違い）・MIL（ミラノの都市コードで空港ではない）・
CST（該当なし）がそのまま収集対象になり、出発日まで毎日失敗し続けていた。
利用者には理由の分からない「取れないモニター」が残るだけだった。
"""
import pytest
from pydantic import ValidationError

from app import airports
from app.api.schemas import WatchCreate


def _create(origin="NRT", destination="SAN", **kw):
    return WatchCreate(origin=origin, destination=destination,
                       departure_date="2026-12-01", **kw)


def _message(origin, destination):
    with pytest.raises(ValidationError) as e:
        _create(origin, destination)
    return e.value.errors()[0]["msg"].replace("Value error, ", "")


# ----------------------------------------------------------------------
# マスタ
# ----------------------------------------------------------------------

def test_master_is_loaded_and_large_enough():
    assert airports.count() > 3000
    assert airports.generated_on()


@pytest.mark.parametrize("code", ["NRT", "HND", "SAN", "LAX", "SFO", "SEA",
                                  "PDX", "KIX", "FUK", "ARN", "HAN"])
def test_real_airports_exist(code):
    assert airports.exists(code)


@pytest.mark.parametrize("code", ["NAR", "CST", "MIL", "SND", "XXX"])
def test_codes_that_caused_failures_do_not_exist(code):
    """実際に収集を失敗させたコード。定期便のある空港ではない。"""
    assert not airports.exists(code)


def test_nearby_airport_rule_targets_are_all_valid():
    """設定の近接空港が全てマスタにあること。

    片方でも欠けると、近接展開で作られたルートが検証を通らない空港を
    指すことになる。
    """
    from app.config import NEARBY_AIRPORTS
    for origin, others in NEARBY_AIRPORTS.items():
        assert airports.exists(origin), origin
        for other in others:
            assert airports.exists(other), other


# ----------------------------------------------------------------------
# 登録時の検証
# ----------------------------------------------------------------------

def test_valid_route_is_accepted():
    w = _create("NRT", "SAN")
    assert (w.origin, w.destination) == ("NRT", "SAN")


def test_lowercase_is_normalised():
    assert _create("nrt", "san").origin == "NRT"


def test_unknown_code_is_rejected_with_the_code_in_the_message():
    msg = _message("FUK", "NAR")
    assert "NAR" in msg
    assert "見つかりません" in msg


def test_city_code_names_the_airports_to_use():
    """都市コードは拒否するが、どれを指せばよいかまで伝える。

    黙って代表空港に読み替えない — 利用者が頼んでいないルートを監視する
    ことになる。
    """
    msg = _message("TYO", "SAN")
    assert "都市コード" in msg
    assert "HND" in msg and "NRT" in msg


def test_city_code_with_a_single_airport_still_helps():
    msg = _message("HND", "MIL")
    assert "MXP" in msg


def test_format_error_is_kept_separate():
    """3文字でないものは、マスタを引く前に形式で弾く。"""
    assert "3文字" in _message("TOKYO", "SAN")
    assert "3文字" in _message("N", "SAN")


def test_airport_code_that_shadows_a_metro_code_resolves_to_the_airport():
    """実在の空港コードが都市コードと綴りを共有していても取り違えない。

    検証は「空港として存在するか」を先に見る。
    """
    shared = [c for c in ("ABA", "PEI", "HIA") if airports.exists(c)]
    assert shared, "前提が崩れている（衝突する組がマスタから消えた）"
    for code in shared:
        assert _create("NRT", code).destination == code


# ----------------------------------------------------------------------
# API 経由
# ----------------------------------------------------------------------

def test_api_rejects_unknown_airport_and_says_why():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    res = client.post("/api/watches", json={
        "origin": "FUK", "destination": "NAR", "departure_date": "2026-12-01"})
    assert res.status_code == 422
    assert "NAR" in " ".join(d["msg"] for d in res.json()["detail"])


def test_master_is_served_to_the_browser():
    """補完と検証が同じファイルを見ていること。

    別々のリストだと、画面で選べるのにサーバが拒否する食い違いが起きる。
    """
    from fastapi.testclient import TestClient
    from app.main import app

    body = TestClient(app).get("/static/airports.json").json()
    assert body["count"] == airports.count()
    assert {a[0] for a in body["airports"]} == set(
        airports._data()["airports"])


def test_served_rows_match_what_the_frontend_reads():
    """配信する行の形が static/js/airports.js の読み方と一致していること。

    生成スクリプトが列を足したり順序を変えたりしても、JS 側は添字で読む
    ので**静かにずれる**。ここで固定する。
    """
    import json

    raw = json.loads(airports.DATA_PATH.read_text(encoding="utf-8"))
    assert raw["fields"] == ["code", "name", "city", "country",
                             "size", "aliases", "runway_ft"]
    for row in raw["airports"]:
        assert len(row) == 7
    sample = next(r for r in raw["airports"] if r[0] == "SFO")
    code, name, city, country, size, aliases, runway = sample
    assert (code, city, country) == ("SFO", "San Francisco", "US")
    assert size in ("l", "m", "s")
    assert isinstance(aliases, list)
    assert runway > 3000            # 大型機が入る空港。並び順の根拠になる


def test_japanese_search_terms_are_present():
    """日本語で引けること。keywords 由来なので、生成条件を変えると落ちる。"""
    def aliases(code):
        return airports.get(code)["aliases"]

    assert any(a.startswith("東京") for a in aliases("HND"))
    assert any(a.startswith("東京") for a in aliases("NRT"))   # 成田も「東京」で出す
    assert any(a.startswith("成田") for a in aliases("NRT"))
    # 英語でも引けること（非 ASCII を優先しすぎて英語名が落ちない）
    assert any("Tokyo" in a for a in aliases("NRT")) or "Tokyo" in airports.get("NRT")["name"]


def test_metro_codes_cover_the_cities_we_care_about():
    for metro, expected in (("TYO", {"HND", "NRT"}), ("OSA", {"ITM", "KIX"})):
        assert set(airports.metro_members(metro)) == expected
