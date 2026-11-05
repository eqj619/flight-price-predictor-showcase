"""ダッシュボードの HTML 生成。**差し替えが当たらなくなるのを捕まえる。**

`build_dashboard.py` は `dashboard/dashboard.js` の日本語の文字列を、
**文字どおりの一致**で S.xxx に差し替えて2言語ぶんを作る。dashboard.js を
編集すると差し替え元と食い違い、`patched_js()` が SystemExit を投げる。

この壊れ方は本番では**毎朝8時のクラウド側ルーティンの中**で起きる。
手元の試験で先に鳴らないと、翌朝ダッシュボードが更新されないことで
初めて気づくことになる。
"""
import importlib
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))


@pytest.fixture
def bd():
    import build_dashboard
    return importlib.reload(build_dashboard)


def _ops(rows):
    return {"kpi": rows, "drops": [
        {"o": "NRT", "d": "LAX", "dep": "2026-11-10", "pct": -32.0,
         "from": 1200.0, "to": 816.0}],
        "meta": {"generated_at": "2026-09-10T08:00:00Z", "watches": 122,
                 "routes": 417, "research_routes": 225, "recs": 66684,
                 "series": 463, "dropped": 427, "total": 122,
                 "creators": 49, "subs": 3, "done": 45}}


def _row(date, **over):
    base = {
        "date": date, "watches_total": 120, "watches_created_last_24h": 2,
        "distinct_creators": 40, "watches_accessed_last_7d": 30,
        "routes_active": 420, "snapshots_real": 60000, "fetch_errors": 0,
        "series_completed": 40, "push_subscriptions": 3,
        "prediction_abs_error_pct_near": 4.1, "prediction_bias_pct_near": 1.2,
        "prediction_band_hit_pct_near": 66.9,
        "prediction_abs_error_pct": 0.7, "prediction_band_hit_pct": 78.0,
        "prediction_abs_error_p90_pct_near": None,
        "prediction_bias_p90_pct_near": None,
    }
    base.update(over)
    return base


# 裾を測りはじめる前の日と、測りはじめてからの日。
ROWS = [_row("2026-09-08"), _row("2026-09-09"),
        _row("2026-09-10", prediction_abs_error_p90_pct_near=18.4,
             prediction_bias_p90_pct_near=15.1)]


def test_the_patches_still_apply(bd):
    """**dashboard.js を編集したらここが鳴る。**"""
    js = bd.patched_js()
    assert "S.a1_t" in js and "S.a1_s3" in js


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_both_languages_build(bd, lang):
    bd.PAYLOAD = json.dumps(bd.to_payload(_ops(ROWS)), ensure_ascii=False)
    html = bd.build(lang)
    assert html.startswith("<title>")
    assert "aerrp" in html and "abiasp" in html


def test_the_tail_reaches_the_payload(bd):
    """**P90 が画面まで運ばれること。** 測っても運ばなければ意味がない。"""
    kpi = bd.to_payload(_ops(ROWS))["kpi"]
    assert [r["aerrp"] for r in kpi] == [None, None, 18.4]
    assert [r["abiasp"] for r in kpi] == [None, None, 15.1]


def test_days_before_we_measured_the_tail_stay_null(bd):
    """**0 に化けさせない。** 「測る前」と「値がゼロ」は違う。

    0 で埋めるとグラフが原点まで落ち、裾が縮んだように見える。
    """
    kpi = bd.to_payload(_ops(ROWS))["kpi"]
    assert kpi[0]["aerrp"] is None
    assert kpi[0]["aerr"] == 4.1        # 中央値のほうは前からある


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_the_tail_is_explained_in_both_languages(bd, lang):
    """数字だけ出して説明しない、をやらない（2026-09-05 の指示）。

    説明文の数字は当日のデータから作る。差し込み口が消えると、文章の
    ほうが固定値になって**いつか嘘になる**。
    """
    for slot in ("{p}", "{w}", "{m}", "{v}"):
        assert slot in bd.STRINGS[lang]["a3_x"], slot
    # 測る前の日のための文言も要る。
    assert bd.STRINGS[lang]["a3_none"]


def test_the_tail_has_its_own_chart(bd):
    """**中央値と同じ縦軸に載せない。**

    P90 は中央値の6〜7倍（実測 55.5% 対 8.4%）。同じ軸だと中央値の推移が
    下端に潰れ、8月24日の引き直しの効果が読めなくなる。
    """
    js = bd.patched_js()
    assert "S.a3_t" in js
    # 誤差と偏りのグラフには裾を混ぜない。
    a1 = js.split("S.a1_t")[1].split("S.a3_t")[0]
    assert "aerrp" not in a1 and "abiasp" not in a1


def test_every_string_exists_in_both_languages(bd):
    assert set(bd.STRINGS["ja"]) == set(bd.STRINGS["en"])


# ── 文言に数字を焼き込まない ──────────────────────────────────
#
# **2026-09-10 に見つけた。** 「期間と件数は毎日変わる。文言に焼き込まず、
# データから作る」とコメントに書いてあるのに、実際に埋めていたのは
# eyebrow だけだった。本文と脚注は最初に書いた日の値のまま10日間出ており、
# グラフが32日ぶんを描いている隣で「23日ぶん」「44,178 件」と言っていた。
# **画面が自分の数字と食い違うのがいちばんまずい。**



@pytest.mark.parametrize("lang", ["ja", "en"])
def test_the_prose_counts_match_the_data(bd, lang, tmp_path, monkeypatch):
    """本文の日数・件数が、グラフの元データと一致すること。"""
    data = tmp_path / "ops.json"
    data.write_text(json.dumps(_ops(ROWS)), encoding="utf-8")
    monkeypatch.setattr("sys.argv",
                        ["build", "--data", str(data), "--out-dir", str(tmp_path)])
    bd.main()

    html = (tmp_path / ("fpp-dashboard.html" if lang == "ja"
                        else "fpp-dashboard.en.html")).read_text(encoding="utf-8")
    days, recs = len(ROWS), f"{_ops(ROWS)['meta']['recs']:,}"
    assert (f"{days}日ぶん" in html) or (f"from {days} days" in html)
    assert recs in html
    assert str(_ops(ROWS)["meta"]["series"]) in html
    # 埋め残しが無いこと。**残ると画面に波括弧がそのまま出る。**
    for slot in ("{days}", "{span}", "{recs}", "{series}", "{dropped}",
                 "{research}", "{acc_days}"):
        assert slot not in html, slot


def test_the_accuracy_day_count_follows_the_data(bd, tmp_path, monkeypatch):
    """精度の日数は「aerr が入っている日」を数える。**総日数ではない。**"""
    rows = [_row("2026-09-06", prediction_abs_error_pct_near=None),
            _row("2026-09-07", prediction_abs_error_pct_near=None)] + ROWS
    data = tmp_path / "ops.json"
    data.write_text(json.dumps(_ops(rows)), encoding="utf-8")
    monkeypatch.setattr("sys.argv",
                        ["build", "--data", str(data), "--out-dir", str(tmp_path)])
    bd.main()

    html = (tmp_path / "fpp-dashboard.html").read_text(encoding="utf-8")
    assert "5日ぶん" in html          # 全5日のうち、精度が入っているのは3日
    assert "計測を始めたため3日ぶん" in html


# ── 買い時の検証（2026-10-12）────────────────────────────────
#
# 精度欄が「明日の価格を何%で当てたか」しか出しておらず、アプリの目的
# （底値の時期を言い当てる）とずれていた。先頭に「買い時の検証」を置き、
# 点の誤差は技術指標として畳んだ。

_BUY = {
    "as_of": "2026-10-12", "series": 72, "series_all": 120,
    "departures": ["2026-08-23", "2026-10-11"], "obs_start_median": 84,
    "rise_dbd": 52, "valley": [52, 59], "near_pct": 5,
    "bottoms": [{"label": "76+", "lo": 76, "hi": None, "n": 11, "valley": False},
                {"label": "52-59", "lo": 52, "hi": 59, "n": 10, "valley": True}],
    "buy_days": [{"dbd": 55, "n": 72, "median_pct": 13.4, "mean_pct": 19.1,
                  "within_pct": 26},
                 {"dbd": 52, "n": 72, "median_pct": 14.5, "mean_pct": 20.0,
                  "within_pct": 17}],
    "best_dbd": 55,
    "verdicts": {"since": "2026-10-12", "evaluable": 0, "by_verdict": {}},
}


def test_the_buy_timing_reaches_the_payload(bd):
    ops = _ops(ROWS)
    ops["buy_timing"] = _BUY
    assert bd.to_payload(ops)["buy"] == _BUY


def test_an_old_feed_without_buy_timing_is_null(bd):
    """**古い取得口には無い。** 無いものは null で運び、画面側が「まだ」と言う。"""
    assert bd.to_payload(_ops(ROWS))["buy"] is None


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_buy_timing_comes_first_and_point_error_is_folded(bd, lang):
    ops = _ops(ROWS)
    ops["buy_timing"] = _BUY
    payload = bd.to_payload(ops)
    bd.PAYLOAD = json.dumps(payload)
    bd.fill_strings(payload)
    html = bd.build(lang)
    t = bd.STRINGS[lang]
    # 節の順: 買い時の検証 → 利用状況。点の誤差は <details class="tech"> の中。
    assert html.index(t["h2_buy"]) < html.index(t["h2_usage"])
    tech = html.index('<details class="tech">')
    assert tech < html.index('id="accuracy"')
    assert html.index(t["h2_buy"]) < tech


def test_the_buy_section_draws_no_japanese_into_english(bd):
    """**新しい描画コードは文言を S から取る。** 日本語を書くと英語版に漏れる。"""
    js = bd.patched_js()
    start = js.index("/* ── 買い時の検証")
    end = js.index("/* ── 折れ線（1系列・面つき）")
    code = "\n".join(line.split("//")[0] for line in js[start:end].splitlines())
    import re
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    assert not re.search(r"[ぁ-んァ-ヶ一-龠]", code)
