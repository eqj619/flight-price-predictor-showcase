"""為替レート（USD/JPY）。日本語表示のときだけ使う。

出典は ECB の参照レート（Frankfurter 経由、API キー不要）。**平日のみ公表**
なので、週末・祝日は直前の営業日の値が返る。それでよい — 表示のための換算に
1日のずれは効かない。

**表示に使うのは常に1つのレート（最新）で、観測日ごとのレートは使わない。**
実測: 為替の日次変化は平均 0.24% で運賃の 1.61% の約15%だが、3か月では
4.6% の幅がある。同じ $700 が ¥109,676〜¥114,737 で動く。日ごとに換算すると
**運賃が1円も動いていないのにグラフが動く**ことになり、このアプリの目的
（いつ上がる・下がるか）を損なう。1つのレートで割り戻せば線の形は変わらない。

**取得に失敗しても何も壊さない。** 最後に取れた値を使い、それも無ければ
JPY 表示自体を出さない（USD のまま）。為替は補助的な表示であって、
価格収集や予測の前提ではない。
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import date
from typing import Optional

from app.db import repository

logger = logging.getLogger("app.fx")

PAIR = "USD/JPY"
SOURCE_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=JPY"
TIMEOUT_SECONDS = 15
# **User-Agent を明示する。** urllib の既定（Python-urllib/x.y）だと 403 で
# 弾かれる。curl では通るので、ネットワークではなく UA での拒否。
USER_AGENT = "flight-price-predictor/1.0 (+https://flights.suosolutions.com)"

# 明らかにおかしい値を弾く。桁を取り違えた値をそのまま表示すると、
# 利用者に「1万円の航空券」や「1億円の航空券」を見せることになる。
MIN_RATE, MAX_RATE = 50.0, 500.0


def fetch() -> Optional[dict]:
    """最新レートを取りに行く。失敗したら None。**例外は投げない。**"""
    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as res:
            body = json.loads(res.read().decode("utf-8"))
        rate = float(body["rates"]["JPY"])
        as_of = body["date"]
    except Exception:
        # **ここは広く捕まえてよい数少ない場所。** 為替は表示の補助でしかなく、
        # 落ちても価格収集・予測・画面のどれも壊れない（USD に倒れる）。
        # 一方 urlopen の失敗経路は URLError / HTTPError / OSError /
        # TimeoutError / http.client の各種と多く、列挙すると必ず漏れる。
        # 実際 (URLError, HTTPError, TimeoutError, KeyError, ValueError,
        # TypeError) と書いていて OSError が素通りした。
        logger.exception("為替レートの取得に失敗")
        return None

    if not (MIN_RATE <= rate <= MAX_RATE):
        logger.error("為替レートが想定の範囲外: %s", rate)
        return None
    return {"rate": rate, "date": as_of}


def refresh() -> Optional[dict]:
    """取得して保存する。日次ジョブから呼ぶ。"""
    latest = fetch()
    if latest is None:
        return None
    repository.save_fx_rate(PAIR, latest["date"], latest["rate"])
    logger.info("為替を更新: 1 USD = %s JPY (%s)", latest["rate"], latest["date"])
    return latest


def current() -> Optional[dict]:
    """表示に使うレート。保存済みの最新を返す。無ければ None。"""
    return repository.latest_fx_rate(PAIR)
