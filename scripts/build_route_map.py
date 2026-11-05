#!/usr/bin/env python3
"""監視中の航空路線マップ（日英）を生成する。**開発時にだけ実行する。**

    .venv/bin/python scripts/build_route_map.py --data /path/to/map-data.json

**日英を1つのスクリプトから出す。** 同じ内容を2つの HTML で手作業維持すると、
データを更新したときに片方だけ古くなる。文言だけを STRINGS に分け、
構造・投影・配色は共有する。

入力 map-data.json の作り方（本番 DB と公開データから組み立てる）:

    # 1. 稼働中のルート
    sqlite3 -csv /var/lib/fpp/flights.db "
      SELECT origin, destination, COUNT(*), COUNT(DISTINCT departure_date),
             (SELECT COUNT(*) FROM snapshots s WHERE s.origin=r.origin
               AND s.destination=r.destination AND s.source='real')
      FROM monitored_routes r WHERE active=1
      GROUP BY origin, destination;" > routes.csv

    # 2. 空港の座標（OurAirports・パブリックドメイン）
    curl -o airports.csv https://davidmegginson.github.io/ourairports-data/airports.csv

    # 3. 陸地の輪郭（Natural Earth 110m・パブリックドメイン）
    curl -o land.json https://raw.githubusercontent.com/nvkelso/\\
natural-earth-vector/master/geojson/ne_110m_land.geojson

    # 4. --routes / --airports / --land を渡して組み立て
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 主要回廊の定義。ここを変えると図の「その他」の内訳も変わる。
CORE = ["HND", "NRT", "SAN", "LAX", "SFO", "SEA", "PDX"]

STRINGS = {
    "ja": {
        "lang": "ja",
        "title": "監視中の航空路線",
        "eyebrow": "flight-price-predictor · {date}",
        "h1": "監視中の航空路線",
        "lede": "毎日 03:00（太平洋時間）に価格を取得している区間。太平洋を中心に置いた"
                "正距円筒図法で、線の太さと色がその区間の収集ルート数を表す。",
        "stat_routes": "監視中のルート", "stat_legs": "区間",
        "stat_airports": "空港", "stat_records": "実測レコード",
        "stat_core": "西海岸⇄東京", "stat_core_sub": "ルート {n}",
        "map_alt": "監視中の航空路線を示す世界地図",
        "key_ramp": "ルート数 少 → 多",
        "key_dot": "空港（円の大きさ＝発着ルート数）",
        "key_hint": "表の行にカーソルを合わせるとその区間が光る",
        "caption": "航路は大圏（最短経路）で描いている。正距円筒図法では直線が最短経路に"
                   "ならないため、直線で結ぶと実際に飛ぶ経路とずれる。陸地の輪郭は "
                   "Natural Earth（パブリックドメイン）、空港の座標は OurAirports（同）。",
        "h2": "区間の一覧",
        "sub": "収集ルート数の多い順。ルート数は「出発地・到着地・出発日」の組で数えている"
               "ため、同じ区間でも出発日が違えば別のルートになる。",
        "th_leg": "区間", "th_kind": "区分", "th_routes": "ルート",
        "th_dates": "出発日", "th_records": "実測", "th_bar": "ルート数",
        "tag_core": "主要回廊", "tag_other": "その他",
        "split": "<b>{total} ルートのうち {core} 本（{pct}%）が米西海岸⇄東京の "
                 "{core_legs} 区間に集中している。</b>残る {rest} 本は {rest_legs} 区間に"
                 "散らばり、1区間あたり平均 {avg} 本。学習データとしての厚みは、"
                 "いまのところ回廊側の出発日の多さで稼いでいる。",
        "footer": "収集対象は稼働中のルートのみ。取得できない区間として自動停止したものと、"
                  "実在しない空港コード（{missing}）で登録され座標を特定できないものは除いてある。"
                  "<br><b>{research} ルートは値動きを調べるために我々が登録した観測用で、"
                  "利用者の需要ではない。</b>米西海岸⇄東京の4区間に集中しているため、"
                  "その区間の線の太さは利用者の関心より太く出ている。",
    },
    "en": {
        "lang": "en",
        "title": "Monitored Flight Routes",
        "eyebrow": "flight-price-predictor · {date}",
        "h1": "Monitored Flight Routes",
        "lede": "Every leg whose fares are collected daily at 03:00 Pacific. "
                "Drawn on an equirectangular projection centred on the Pacific; "
                "line weight and colour show how many routes each leg carries.",
        "stat_routes": "Routes watched", "stat_legs": "Legs",
        "stat_airports": "Airports", "stat_records": "Observations",
        "stat_core": "West Coast ⇄ Tokyo", "stat_core_sub": "{n} routes",
        "map_alt": "World map showing the monitored flight routes",
        "key_ramp": "Routes: few → many",
        "key_dot": "Airport (circle size = routes in and out)",
        "key_hint": "Hover a table row to light up that leg",
        "caption": "Legs follow great circles. On an equirectangular projection a "
                   "straight line is not the shortest path, so drawing one would "
                   "misplace the route. Coastlines from Natural Earth (public domain); "
                   "airport coordinates from OurAirports (public domain).",
        "h2": "Every leg",
        "sub": "Ordered by the number of routes collected. A route is one "
               "origin–destination–departure-date combination, so the same leg "
               "counts once per departure date.",
        "th_leg": "Leg", "th_kind": "Group", "th_routes": "Routes",
        "th_dates": "Dep. dates", "th_records": "Observations", "th_bar": "Share",
        "tag_core": "Core corridor", "tag_other": "Other",
        "split": "<b>{core} of {total} routes ({pct}%) sit on the {core_legs} legs "
                 "between the US West Coast and Tokyo.</b> The remaining {rest} are "
                 "spread across {rest_legs} legs — {avg} per leg on average. For now "
                 "the depth of the training data comes from the many departure dates "
                 "on the corridor, not from breadth of markets.",
        "footer": "Only active routes are shown. Legs stopped automatically because "
                  "nothing could be collected, and legs registered with codes that are "
                  "not real airports ({missing}) and therefore have no coordinates, "
                  "are excluded."
                  "<br><b>{research} of these routes are instrumentation we registered "
                  "ourselves to study how fares move, not user demand.</b> They sit on "
                  "the four US West Coast \u21c4 Tokyo legs, so those lines are drawn "
                  "heavier than user interest alone would make them.",
    },
}


def render(data: dict, t: dict, generated: str, missing: list[str],
           research: int) -> str:
    """1言語ぶんの HTML を組み立てる。構造と配色は言語によらず共有する。"""
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    tpl = (ROOT / "scripts" / "route-map.template.html").read_text(encoding="utf-8")
    strings = json.dumps(
        {**t, "eyebrow": t["eyebrow"].format(date=generated),
         "footer": t["footer"].format(missing=" / ".join(missing) or "—",
                                      research=research),
         "core": CORE},
        separators=(",", ":"), ensure_ascii=False)
    return (tpl
            .replace("__LANG__", t["lang"])
            .replace("__TITLE__", t["title"])
            .replace("__STRINGS__", strings)
            .replace("__DATA__", payload))


def build_data(routes_csv: Path, airports_csv: Path, land_json: Path) -> tuple[dict, list[str]]:
    coords = {}
    for r in csv.DictReader(airports_csv.open(encoding="utf-8")):
        if r["iata_code"]:
            try:
                coords[r["iata_code"]] = (float(r["latitude_deg"]),
                                          float(r["longitude_deg"]),
                                          r["municipality"] or r["name"],
                                          r["iso_country"])
            except ValueError:
                pass

    rows = [r for r in csv.reader(routes_csv.open()) if len(r) == 5]
    missing = sorted({c for r in rows for c in (r[0], r[1]) if c not in coords})

    tally: dict[str, dict] = {}
    for o, d, n, _dd, rec in rows:
        for c in (o, d):
            t = tally.setdefault(c, {"routes": 0, "records": 0})
            t["routes"] += int(n)
            t["records"] += int(rec)

    codes = sorted({c for r in rows for c in (r[0], r[1])} & coords.keys())
    airports = [{"code": c, "lat": coords[c][0], "lon": coords[c][1],
                 "city": coords[c][2], "country": coords[c][3],
                 "routes": tally[c]["routes"], "records": tally[c]["records"]}
                for c in codes]
    legs = [{"o": o, "d": d, "routes": int(n), "dates": int(dd), "records": int(rec)}
            for o, d, n, dd, rec in rows if o in coords and d in coords]

    land = json.loads(land_json.read_text(encoding="utf-8"))

    def thin(c):
        if isinstance(c[0], (int, float)):
            return [round(c[0], 2), round(c[1], 2)]
        return [thin(x) for x in c]

    polys, types = [], []
    for f in land["features"]:
        g = f["geometry"]
        types.append(g["type"])
        polys.append(thin(g["coordinates"]) if g["type"] == "Polygon"
                     else [thin(p) for p in g["coordinates"]])

    return ({"airports": airports, "legs": legs,
             "land": {"types": types, "polys": polys},
             "totals": {"legs": len(legs),
                        "routes": sum(l["routes"] for l in legs),
                        "airports": len(airports),
                        "records": sum(l["records"] for l in legs)}},
            missing)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, help="組み立て済みの map-data.json")
    ap.add_argument("--routes", type=Path)
    ap.add_argument("--airports", type=Path)
    ap.add_argument("--land", type=Path)
    ap.add_argument("--missing", default="", help="座標を特定できなかったコード（カンマ区切り）")
    ap.add_argument("--date", required=True, help="データの取得日 YYYY-MM-DD")
    # **注記に要る。** 調査用ルートは利用者の需要ではないので、総数に
    # 混ぜたまま黙っていると、地図が demand を表しているように読める。
    ap.add_argument("--research", type=int, default=0,
                    help="うち観測用に自分で登録したルート数")
    args = ap.parse_args()

    if args.data:
        data = json.loads(args.data.read_text(encoding="utf-8"))
        missing = [m for m in args.missing.split(",") if m]
    else:
        data, missing = build_data(args.routes, args.airports, args.land)

    for lang, t in STRINGS.items():
        out = ROOT / "docs" / ("route-map.html" if lang == "ja" else f"route-map.{lang}.html")
        out.write_text(render(data, t, args.date, missing, args.research),
                       encoding="utf-8")
        print(f"  {out.relative_to(ROOT)}  {out.stat().st_size / 1024:.0f} KB  ({lang})")
    print(f"\n  {data['totals']}  / 座標なし: {missing or 'なし'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
