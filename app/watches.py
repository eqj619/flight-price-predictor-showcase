"""モニターと収集対象ルートの対応づけ。

`expand_routes` はモニター登録時にしか走らないので、そのままだと
「近接空港ルールを切り替えた」「NEARBY_AIRPORTS を増やした」といった変更が
既存モニターに反映されない。張り替えの手順を1か所にまとめ、API・CLI の
どちらからも同じ経路を通るようにする。
"""
from dataclasses import dataclass

from app.db import repository
from app.routing import expand_routes


@dataclass(frozen=True)
class SyncResult:
    routes: list[tuple[str, str]]
    linked: int         # 新たに紐付いた数
    unlinked: int       # 外れた数


def sync_watch_routes(watch: repository.Watch) -> SyncResult:
    """このモニターが必要とするルートに、紐付けを合わせる。

    足りなければ足し、余っていれば外す。**価格履歴（snapshots）と
    monitored_routes の行は消さない。** 近接空港ルールを切って再度入れた
    ときに、過去のトレンドをそのまま見せられるようにするため。
    """
    pairs = expand_routes(watch.origin, watch.destination,
                          nearby_airports=watch.nearby_airports)
    route_ids = [
        repository.upsert_monitored_route(origin, destination,
                                          watch.departure_date,
                                          watch.return_date)
        for origin, destination in pairs
    ]

    linked = repository.link_watch_routes(watch.id, route_ids)
    unlinked = repository.unlink_watch_routes(watch.id, route_ids)
    if unlinked:
        # 誰からも参照されなくなったルートは収集を止める。
        repository.deactivate_orphan_routes()

    return SyncResult(routes=pairs, linked=linked, unlinked=unlinked)
