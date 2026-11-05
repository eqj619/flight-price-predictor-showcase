"""モニター1件 -> 収集対象ルート群への展開（2.3 の近接空港ルール）。

Phase 2 の収集ジョブと Phase 4 の登録 API の両方から使うため、CLI ではなく
ここに置く。
"""
from typing import Optional

from app.config import NEARBY_AIRPORTS, get_settings


def _is_nearby_pair(a: str, b: str) -> bool:
    """a と b が近接空港どうしか（LAX-SAN、HND-NRT）。"""
    return b in NEARBY_AIRPORTS.get(a, []) or a in NEARBY_AIRPORTS.get(b, [])


def expand_routes(origin: str, destination: str,
                  nearby_airports: bool = False,
                  max_routes: Optional[int] = None) -> list[tuple[str, str]]:
    """1件のモニターが必要とする収集対象ルートに展開する。

    近接空港は出発側にも到着側にも効かせ、**組み合わせで**展開する。
    SAN->HND なら SAN/LAX × HND/NRT の4通りを見る。

    以前は「片側ずつ」に絞っていたが、HND<->NRT を足した時点でその判断は
    誤りになった。SAN->HND を片側ずつ展開すると LAX->NRT が候補から漏れる
    ところ、実測で最安だった ZIPAIR はまさに NRT-LAX 路線を飛んでいる
    （無指定検索 ANA $2,172 に対し ZG 明示指定で $494）。取りこぼす額が
    大きすぎる。

    掛け算で伸びるので `max_routes_per_watch` で歯止めをかける。
    要求されたルート自体は必ず先頭に残す。

    近接空港どうしを結ぶ便（LAX-SAN 間、HND-NRT 間）は展開しない。陸路等で
    移動する前提なので、そこに航空券は要らない。ただし利用者が明示的に
    その区間をモニターした場合（SAN->LAX など）は当然それを収集する。

    SAN 専用のハードコードにはせず config.NEARBY_AIRPORTS を引くだけに
    してあるので、他の近接ペアは設定を足すだけで増やせる。
    """
    routes = [(origin, destination)]
    if not nearby_airports:
        return routes

    if max_routes is None:
        max_routes = get_settings().max_routes_per_watch

    # 展開先の候補。相手側と同じ空港になるものは外す（LAX->LAX を作らない）。
    origins = [origin] + [a for a in NEARBY_AIRPORTS.get(origin, [])
                          if a != destination]
    destinations = [destination] + [a for a in NEARBY_AIRPORTS.get(destination, [])
                                    if a != origin]

    for o in origins:
        for d in destinations:
            if o == d or (o, d) in routes:
                continue
            if _is_nearby_pair(o, d):
                continue        # 近接空港どうしの区間は陸路で移動する
            routes.append((o, d))

    return routes[:max_routes]
