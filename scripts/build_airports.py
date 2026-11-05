#!/usr/bin/env python3
"""空港マスタを OurAirports から生成する。**開発時にだけ実行する。**

生成物 `static/airports.json` はリポジトリにコミットする。実行時に外部へ
取りに行かないのは、(1) 起動が外部サービスの可用性に依存しないため、
(2) 収集ジョブ以外の外向き通信を作らないため、(3) 「ビルド工程なし」の
方針を保つため。

出典: https://ourairports.com/data/ — **パブリックドメイン**（表示義務・
継承条件なし。公開リポジトリでも制約にならない）。

    .venv/bin/python scripts/build_airports.py

絞り込みは `IATA コードあり` かつ `定期便あり` の2条件だけ。これで実際の
誤入力（NAR = コロンビアの小空港、CST = 水上機基地、MIL = 都市コードで
空港ではない）が正確に落ちることを実測で確認している。**空港の種類で
足切りしない** — small_airport にも定期便のある空港があり、落とすと
正当な入力を拒否することになる。
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

SOURCE_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
RUNWAYS_URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"
OUT_PATH = Path(__file__).resolve().parent.parent / "static" / "airports.json"

# 種類の1文字表記。表示順（大きい空港を上に出す）に使う。
SIZE_RANK = {"large_airport": "l", "medium_airport": "m"}
_METRO = re.compile(r"^[A-Z]{3}$")
# 非 ASCII を優先しつつ英語の別名も残す。4 だと NRT が
# ['成田国際空港','成田空港','東京都','東京成田'] で埋まり、"tokyo" で
# 成田が出なくなる（実測で判明）。
MAX_ALIASES = 6


def fetch(url: str) -> str:
    print(f"取得中: {url}")
    with urllib.request.urlopen(url, timeout=120) as res:
        raw = res.read()
    print(f"  {len(raw) / 1024 / 1024:.1f} MB")
    return raw.decode("utf-8")


def aliases_for(row: dict) -> list[str]:
    """検索用の別名。

    `keywords` には旧称・現地語名が入っており、**日本語の空港名もここから
    取れる**（NRT なら「成田国際空港」「東京」）。空港名にすでに含まれる語は
    重複なので落とす。件数を絞るのは配信サイズのため。
    """
    name = row["name"].lower()
    out = []
    for kw in (k.strip() for k in row["keywords"].split(",")):
        if not kw or _METRO.fullmatch(kw):
            continue                      # 都市コードは metros 側で持つ
        if kw.lower() in name:
            continue
        out.append(kw)

    # **非 ASCII の別名を先に採る。** 件数を打ち切る以上、英語名は name と
    # city で拾えるぶん優先度が低い。逆にすると NRT から「東京都」が落ち、
    # 「東京」で検索したときに成田が出なくなる（実際に起きた）。
    out.sort(key=lambda k: k.isascii())
    return out[:MAX_ALIASES]


def longest_runway(csv_text: str) -> dict[str, int]:
    """空港ごとの最長滑走路（フィート）。

    **候補の並び順に使う。** 種類（large/medium）は粗すぎて差がつかない
    （"san" で始まる都市の大空港だけで28件ある）。滑走路長は人気度そのもの
    ではないが、大型機が入る空港ほど利用者の意図する空港である確率が高く、
    コード順に並べるよりは確実に良い。
    """
    out: dict[str, int] = {}
    for r in csv.DictReader(io.StringIO(csv_text)):
        try:
            length = int(r["length_ft"] or 0)
        except ValueError:
            continue
        ident = r["airport_ident"]
        if length > out.get(ident, 0):
            out[ident] = length
    return out


def build(csv_text: str, runways: dict[str, int]) -> dict:
    rows = [r for r in csv.DictReader(io.StringIO(csv_text))
            if r["iata_code"] and r["scheduled_service"] == "yes"]

    airports = []
    metro_members: dict[str, list[str]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: r["iata_code"]):
        code = r["iata_code"]
        airports.append([
            code,
            r["name"],
            r["municipality"],
            r["iso_country"],
            SIZE_RANK.get(r["type"], "s"),
            aliases_for(r),
            runways.get(r["ident"], 0),
        ])
        for kw in (k.strip() for k in r["keywords"].split(",")):
            if _METRO.fullmatch(kw) and kw != code:
                metro_members[kw].append(code)

    # **所属が1件の都市コードも残す。** 「すべての空港」としてまとめる意味は
    # 無いが、MIL や BJS を入力されたときに MXP / PEK を案内できる。
    # 実在の空港コードと綴りが衝突するもの（ABA など）もあるが、検証は
    # 空港として存在するかを先に見るので取り違えは起きない。
    metros = {k: sorted(v) for k, v in sorted(metro_members.items())}

    return {
        "source": "OurAirports (https://ourairports.com/data/) — Public Domain",
        "generated": date.today().isoformat(),
        "filter": "iata_code is set AND scheduled_service = yes",
        "fields": ["code", "name", "city", "country", "size", "aliases", "runway_ft"],
        "count": len(airports),
        "airports": airports,
        "metros": metros,
    }


def main() -> int:
    data = build(fetch(SOURCE_URL), longest_runway(fetch(RUNWAYS_URL)))

    # 生成物が壊れていないことを、書き出す前に確かめる。ここが静かに空に
    # なると、全部の登録が「未知の空港」で弾かれる。
    codes = {a[0] for a in data["airports"]}
    for code in ("NRT", "HND", "SAN", "LAX", "SFO", "SEA", "PDX", "KIX", "FUK"):
        if code not in codes:
            print(f"検証に失敗: {code} が含まれていない", file=sys.stderr)
            return 1
    if len(codes) < 3000:
        print(f"検証に失敗: 件数が少なすぎる ({len(codes)})", file=sys.stderr)
        return 1

    OUT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8")
    size = OUT_PATH.stat().st_size
    print(f"書き出し: {OUT_PATH}  {len(codes):,} 空港 / {size / 1024:.0f} KB")
    print(f"  都市コード: {len(data['metros'])} 件（例 TYO={data['metros'].get('TYO')}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
