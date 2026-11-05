"""API エンドポイント。

トークンは capability として扱う。**認証はしない代わりに、トークンを
知っていること自体が権限**なので、次の3つを守る。

- トークンはサーバが `secrets.token_urlsafe(16)` で発行する（連番 ID 不可）
- `/w/{token}` は noindex。共有 URL が検索エンジンに載らないようにする
- ログにトークンを残さない（app/security.py の伏字フィルタ）

SQL はここに書かない。すべて repository 越し。
"""
import csv
import secrets
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response, status

from app import clock
from app.api.schemas import (PushSubscribe, PushUnsubscribe, RouteRef,
                             WatchCreate, WatchCreated, WatchUpdate)
from app.config import get_settings
from app.db import repository
from app.jobs.scheduler import scheduler
from app.predict.engine import (BASIS_NO_FLIGHTS, BASIS_OBSERVED,
                                BASIS_UNCONFIRMED,
                                BASIS_ROUND_TRIP_PENDING,
                                BASIS_SIMULATED, build_trend, recommend)
from app.routing import expand_routes
from app import fx
from app.security import ip_hash
from app.watches import sync_watch_routes

router = APIRouter(prefix="/api", tags=["watches"])


def _client_ip(request: Request) -> Optional[str]:
    """レート制限に使うクライアント IP。

    **X-Forwarded-For は既定では信用しない。** ヘッダはクライアントが自由に
    付けられるので、信用すると IP あたりの登録上限を毎回別人のふりをして
    素通りできる。信用してよいのは、リバースプロキシがこのヘッダを必ず
    上書きする構成にしたうえで `TRUST_PROXY_HEADERS=true` を明示したときだけ。
    """
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # プロキシが上書きしている前提なので先頭が実クライアント。
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _enforce_route_cap(new_route_count: int) -> None:
    settings = get_settings()
    if repository.count_active_routes() + new_route_count > settings.max_active_routes:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="監視できるルート数が上限に達しています。しばらくしてからお試しください。",
        )


def _enforce_limits(ip: Optional[str], new_route_count: int) -> None:
    """上限に達していたら**明示的に拒否する**。

    黙って収集対象から外すと、ユーザーには「登録できたのに価格が
    増えない」壊れ方に見える。断るなら断ると言う。
    """
    settings = get_settings()
    _enforce_route_cap(new_route_count)

    hashed = ip_hash(ip)
    if hashed is None:
        return

    since = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    if repository.count_watches_created_since(hashed, since) >= settings.max_watches_per_ip_per_day:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"1日に登録できるモニターは{settings.max_watches_per_ip_per_day}件までです。",
        )
    if repository.count_active_watches_for_ip(hashed) >= settings.max_active_watches_per_ip:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"同時に持てるモニターは{settings.max_active_watches_per_ip}件までです。",
        )


@router.post("/watches", response_model=WatchCreated,
             status_code=status.HTTP_201_CREATED)
def create_watch(payload: WatchCreate, request: Request) -> WatchCreated:
    departure_date = payload.departure_date.isoformat()
    # '' が片道。以降ずっとこの規約で通す（DB の列も同じ）。
    return_date = payload.return_date.isoformat() if payload.return_date else ""
    pairs = expand_routes(payload.origin, payload.destination,
                          nearby_airports=payload.nearby_airports)

    _enforce_limits(_client_ip(request), len(pairs))

    route_ids = [
        repository.upsert_monitored_route(origin, destination, departure_date,
                                          return_date)
        for origin, destination in pairs
    ]
    watch = repository.create_watch(
        origin=payload.origin,
        destination=payload.destination,
        departure_date=departure_date,
        return_date=return_date,
        route_ids=route_ids,
        airline_filter=payload.airline_filter or None,
        nearby_airports=payload.nearby_airports,
        ip_hash=ip_hash(_client_ip(request)),
        notify_email=payload.notify_email,
    )

    return WatchCreated(
        token=watch.token,
        url=f"/w/{watch.token}",
        origin=watch.origin,
        destination=watch.destination,
        departure_date=payload.departure_date,
        return_date=payload.return_date,
        expires_at=date.fromisoformat(watch.expires_at),
        routes=_route_refs(pairs, payload.origin, payload.destination),
    )


@router.get("/watches/{token}")
def get_watch(token: str) -> dict:
    watch = repository.get_watch_by_token(token)
    if watch is None:
        # 存在しないトークンと失効したトークンを区別しない
        # （総当たりに情報を与えない）。
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="見つかりません")

    # アクセスされている限り生かす。localStorage を消してもブックマークした
    # URL から復旧できる必要がある。
    repository.touch_watch(token)

    routes = repository.routes_for_watch(token)
    today = clock.today()
    airline_filter = watch.airline_filter or None
    max_minutes = watch.max_total_minutes

    payload_routes = []
    for route in routes:
        # **便が無いと分かった区間には、推定を出さない。**
        # 運航されていない区間（NRT-GMP など）にモデル推定を出すと、
        # 存在しない便の値段を断定的に見せることになる。停止した理由を
        # 伝えるほうが、数字を埋めるより利用者の役に立つ。
        if route.disabled_kind == "no_flights":
            payload_routes.append({
                "origin": route.origin,
                "destination": route.destination,
                "nearby": (route.origin, route.destination)
                          != (watch.origin, watch.destination),
                "active": route.active,
                "disabled_reason": route.disabled_reason,
                "stopped_kind": route.disabled_kind,
                "excluded_by_duration": 0,
                "recommendation": None,
                "summary": {"basis": BASIS_NO_FLIGHTS, "best": None,
                            "avg_price_usd": None, "records": 0,
                            "days_monitored": 0, "evidence_points": 0},
                "series": [],
            })
            continue

        trends = build_trend(route.origin, route.destination,
                             route.departure_date, airline_filter,
                             max_total_minutes=max_minutes,
                             return_date=route.return_date)

        # **この区間の実測を一度も見ていないなら、値段を出さない。**
        #
        # basis=simulated は「この出発日の実測が無い」ではなく
        # 「この区間の実測が1件も無い」という意味で、**路線が存在するか
        # すら確かめられていない**状態。そこに予想価格を出すと、値段が
        # 付いていること自体が「この路線はある」という主張になる
        # （2026-08-30 の指摘）。運航されていない区間だと分かるのは
        # 3日後なので、それまで嘘をつき続けることになる。
        #
        # 別の出発日の実測から転用した推定（transferred）は出してよい。
        # そちらは実測がある＝区間が実在する証拠になっている。
        if trends and all(t.basis == BASIS_SIMULATED for t in trends.values()):
            payload_routes.append({
                "origin": route.origin,
                "destination": route.destination,
                "nearby": (route.origin, route.destination)
                          != (watch.origin, watch.destination),
                "active": route.active,
                "disabled_reason": route.disabled_reason,
                "stopped_kind": route.disabled_kind,
                "excluded_by_duration": 0,
                "recommendation": None,
                "summary": {"basis": BASIS_UNCONFIRMED, "best": None,
                            "avg_price_usd": None, "records": 0,
                            "days_monitored": 0, "evidence_points": 0},
                "series": [],
            })
            continue

        payload_routes.append({
            "origin": route.origin,
            "destination": route.destination,
            "nearby": (route.origin, route.destination)
                      != (watch.origin, watch.destination),
            "active": route.active,
            "disabled_reason": route.disabled_reason,
            "stopped_kind": route.disabled_kind,
            # 何件が所要時間で隠れているかを必ず返す。黙って消すと
            # 「安い便が理由なく消えた」ように見える。
            "excluded_by_duration": repository.count_long_itineraries(
                route.origin, route.destination, route.departure_date,
                max_minutes, return_date=route.return_date),
            "recommendation": recommend(route.origin, route.destination,
                                        route.departure_date, today,
                                        airline_filter,
                                        max_total_minutes=max_minutes,
                                        return_date=route.return_date),
            "summary": _summarize(trends, route.return_date),
            "series": [t.as_dict() for t in trends.values()],
        })

    return {
        "token": watch.token,
        "origin": watch.origin,
        "destination": watch.destination,
        "departure_date": watch.departure_date,
        # '' が片道。画面はこれを見て往復の表記に切り替える。
        "return_date": watch.return_date,
        "airline_filter": watch.airline_filter,
        "max_total_minutes": watch.max_total_minutes,
        "nearby_airports": watch.nearby_airports,
        "created_at": watch.created_at,
        "expires_at": watch.expires_at,
        # 日本語表示のときだけ使う。取得できていなければ null で、
        # 画面は USD のまま出す。
        "fx": fx.current(),
        "latest_check_date": repository.latest_check_date(),
        "next_check": _next_check(),
        "available_airlines": _available_airlines(routes, watch.airline_filter),
        "routes": payload_routes,
    }


def _available_airlines(routes, current_filter: list[str]) -> list[str]:
    """しぼり込みの選択肢。

    このモニターのルートで実際に観測された社に限る。曲線の係数表から候補を
    出すと、その社が飛んでいない路線まで選べてしまう。

    いま選ばれている社は、まだ観測されていなくても候補に残す。そうしないと
    「選んだのに一覧から消えて外せない」状態になる。
    """
    settings = get_settings()
    since = (clock.today()
             - timedelta(days=settings.airline_evidence_days)).isoformat()

    names = set(current_filter)
    for route in routes:
        names.update(repository.airlines_on_route(route.origin,
                                                  route.destination, since))
    return sorted(names)


def _summarize(trends, return_date: str = "") -> dict:
    """ダッシュボードのサマリーカード用の集計。

    実測があるときは実測だけを集計する。実測とモデル推定を混ぜて
    「最安値」を出すと、PI-27-flightTrend と同じ「シミュレーション値を
    実測のように見せるダッシュボード」になる。
    """
    observed = [
        {"price_usd": point.price_usd, "check_date": point.check_date,
         "airline": trend.airline, "stops": trend.stops,
         "via_airport": trend.via_airport}
        for trend in trends.values() for point in trend.observed
    ]

    if observed:
        best = min(observed, key=lambda p: p["price_usd"])
        return {
            "basis": BASIS_OBSERVED,
            "best": best,
            "avg_price_usd": round(sum(p["price_usd"] for p in observed) / len(observed), 2),
            "records": len(observed),
            "days_monitored": max((t.observed_days for t in trends.values()), default=0),
            "evidence_points": 0,
        }

    if return_date:
        # **往復は推定に落とさない。** trends が空なのは「まだ観測が無い」
        # という意味であって、「推定を出している」ではない。ここを
        # BASIS_SIMULATED のままにすると、画面が「モデルによる推定です」と
        # 出してしまう（実際には何も出していないのに）。
        return {"basis": BASIS_ROUND_TRIP_PENDING, "best": None,
                "avg_price_usd": None, "records": 0, "days_monitored": 0,
                "evidence_points": 0}

    # この出発日の実測ゼロ。推定の最小値を「予測の底値」として返す。
    basis = next((t.basis for t in trends.values()), BASIS_SIMULATED)
    projected = [
        {"price_usd": point.price_usd, "check_date": point.check_date,
         "airline": trend.airline, "stops": trend.stops,
         "via_airport": trend.via_airport}
        for trend in trends.values() for point in trend.projected
    ]
    if not projected:
        return {"basis": basis, "best": None, "avg_price_usd": None,
                "records": 0, "days_monitored": 0, "evidence_points": 0}

    return {
        "basis": basis,
        "best": min(projected, key=lambda p: p["price_usd"]),
        "avg_price_usd": round(sum(p["price_usd"] for p in projected) / len(projected), 2),
        "records": 0,
        "days_monitored": 0,
        # 「別の出発日の実測◯件から」と言えるようにしておく。
        "evidence_points": max((t.evidence_points for t in trends.values()), default=0),
    }


def _next_check() -> dict:
    status = scheduler.status()
    return {
        "enabled": status["enabled"],
        "time": status["schedule"],
        "timezone": status["timezone"],
        "at": status["next_run"],
    }


@router.patch("/watches/{token}", status_code=status.HTTP_204_NO_CONTENT)
def update_watch(token: str, payload: WatchUpdate) -> Response:
    """モニターの設定を変える。省略した項目は変更しない。

    - `airline_filter`: 表示だけでなく、較正と買い時判定の入力も絞られる
    - `max_total_minutes`: 総所要（飛行 + 乗継）の上限。0 で上限なし。
      これも表示だけでなく較正と買い時判定の入力から外れる
      （「ZIPAIR だけ乗るならいつ買うか」を出せる）。収集対象は変わらない
    - `nearby_airports`: **収集対象そのものが変わる。** 切ると近接空港の
      ルートへの紐付けを外し、誰も見ていなくなったルートは収集を止める
    """
    if repository.get_watch_by_token(token) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="見つかりません")

    if payload.airline_filter is not None:
        repository.update_airline_filter(token, payload.airline_filter)

    if payload.max_total_minutes is not None:
        # 0 は「上限なし」。DB では NULL で持つ。
        repository.update_max_total_minutes(
            token, payload.max_total_minutes or None)

    if payload.nearby_airports is not None:
        repository.set_nearby_airports(token, payload.nearby_airports)
        # 上限は登録時と同じ基準で見る。切る方向なら増えないので素通りする。
        watch = repository.get_watch_by_token(token)
        pairs = expand_routes(watch.origin, watch.destination,
                              nearby_airports=watch.nearby_airports)
        existing = {(r.origin, r.destination)
                    for r in repository.routes_for_watch(token)}
        added = len(set(pairs) - existing)
        if added:
            _enforce_route_cap(added)
        sync_watch_routes(watch)

    repository.touch_watch(token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------
# 一覧（collections）
#
# **モニターと同じ capability。** 一覧のトークンを知っている＝開ける。
# 認証は無い。扱うのが価格と日付だけで個人情報と結びつかないため、
# 利便性を優先した判断（2026-08-11）。
# ----------------------------------------------------------------------

@router.post("/collections", status_code=status.HTTP_201_CREATED)
def create_collection(request: Request) -> dict:
    collection = repository.create_collection(ip_hash=ip_hash(_client_ip(request)))
    return {"token": collection.token, "url": f"/l/{collection.token}",
            "expires_at": collection.expires_at}


@router.get("/collections/{token}")
def read_collection(token: str) -> dict:
    collection = repository.get_collection_by_token(token)
    if collection is None:
        # 存在しないトークンと失効したトークンを区別しない。
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="見つかりません")
    repository.touch_collection(token)

    return {
        "token": collection.token,
        "expires_at": collection.expires_at,
        "watches": [
            {"token": w.token, "origin": w.origin, "destination": w.destination,
             "departure_date": w.departure_date, "expires_at": w.expires_at}
            for w in repository.watches_in_collection(token)
        ],
    }


@router.put("/collections/{token}/watches/{watch_token}",
            status_code=status.HTTP_204_NO_CONTENT)
def add_to_collection(token: str, watch_token: str) -> Response:
    """一覧にモニターを入れる。**冪等**なので、重複して呼ばれても増えない。"""
    if not repository.add_watch_to_collection(token, watch_token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="見つかりません")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/collections/{token}/watches/{watch_token}",
               status_code=status.HTTP_204_NO_CONTENT)
def remove_from_collection(token: str, watch_token: str) -> Response:
    """一覧から外すだけ。**モニター自体は消さない。**"""
    repository.remove_watch_from_collection(token, watch_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/watches/{token}", status_code=status.HTTP_204_NO_CONTENT)
def delete_watch(token: str) -> Response:
    if not repository.delete_watch(token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="見つかりません")
    # どの watch からも参照されなくなったルートは収集を止める。
    # 価格履歴（snapshots）は消さない — 同じルートが再登録されたときに
    # 過去のトレンドを即座に見せられるようにするため。
    repository.deactivate_orphan_routes()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _route_refs(pairs: list[tuple[str, str]],
                origin: str, destination: str) -> list[RouteRef]:
    # 近接空港ルールは出発側にも効くので、到着地だけでは判定できない。
    return [
        RouteRef(origin=o, destination=d, nearby=((o, d) != (origin, destination)))
        for o, d in pairs
    ]


# ============================================================
# 通知の購読（課題4）
#
# **この段階では何も送らない。** 購読を貯めるだけ。送信は段階B。
# ============================================================

@router.get("/push/key")
def push_public_key() -> dict:
    """ブラウザが subscribe() に渡す VAPID 公開鍵。

    **鍵が設定されていなければ enabled=false を返す。** 画面はこれを見て
    購読ボタンを出すかどうかを決める。設定し忘れた状態で「通知を受け取る」
    と見せて、実際には何も来ない事故を防ぐ。
    """
    settings = get_settings()
    if not settings.push_enabled:
        return {"enabled": False, "public_key": None}
    return {"enabled": True, "public_key": settings.vapid_public_key}


@router.post("/push/subscribe", status_code=status.HTTP_201_CREATED)
def push_subscribe(payload: PushSubscribe) -> dict:
    """購読を登録し、1件のモニターに紐付ける。

    **モニターのトークンを知っていることが唯一の認可。** モニター自体が
    capability（URL を知っている＝開ける）なので、通知の購読も同じ強さに
    そろえる。ここだけ厳しくしても、URL を知っていれば中身は見られる。
    """
    if not get_settings().push_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="通知は現在利用できません")

    subscription_id = repository.upsert_push_subscription(
        payload.endpoint, payload.p256dh, payload.auth, payload.lang)
    if not repository.link_push_subscription(subscription_id,
                                             payload.watch_token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="見つかりません")

    return {"watches": repository.watch_tokens_for_subscription(payload.endpoint)}


@router.post("/push/unsubscribe", status_code=status.HTTP_204_NO_CONTENT)
def push_unsubscribe(payload: PushUnsubscribe) -> Response:
    """通知をやめる。**存在しない購読でも 204 を返す。**

    どの endpoint が登録済みかを外から探れないようにするため。
    """
    if payload.watch_token:
        repository.unlink_push_subscription(payload.endpoint, payload.watch_token)
    else:
        repository.delete_push_subscription(payload.endpoint)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/push/status")
def push_status(endpoint: str) -> dict:
    """この端末が、どのモニターを購読しているか。画面の表示に使う。"""
    tokens = repository.watch_tokens_for_subscription(endpoint)
    if tokens:
        repository.touch_push_subscription(endpoint)
    return {"watches": tokens}

# ======================================================================
# 運用ダッシュボードの取得口
# ======================================================================

@router.get("/ops/dashboard.json", include_in_schema=False)
def ops_dashboard(key: str = "") -> dict:
    """毎朝のダッシュボード再生成に要るデータを1回で返す。

    **なぜ要るか。** ダッシュボードを作り直すのはクラウド側のルーティンで、
    そこには本番 DB も S3 の認証情報も無い。渡す口がここだけ必要になる。

    **鍵が未設定なら 404。** 「開いているつもりが無かった」を起こさない
    ため、設定しない限りこのパスは存在しないのと同じにする。

    返すのは集計だけで、モニターのトークンや IP ハッシュは含めない。
    """
    settings = get_settings()
    if not settings.ops_api_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # 長さの違いも漏らさないよう、比較は必ず compare_digest で。
    if not secrets.compare_digest(key, settings.ops_api_key):
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    # **中身は1か所で作る。** S3 への書き出しと、この口と、HTML の口で別々に
    # 組み立てていたため、項目を足すと1か所だけ漏れる恐れがあった
    # （2026-09-10 に、文言を埋める処理が本番の経路から漏れた前例がある）。
    from app.dashboard_publish import build_payload
    return build_payload(settings)


def _ops_kpi_rows(csv_path) -> list[dict]:
    """KPI の CSV をそのまま行の配列にする。**数値は数値で返す。**

    画面側で parseFloat を書かせると、空欄（計測を始める前の日）が 0 に
    化けてグラフが原点まで落ちる。空欄は null のまま渡す。
    """
    path = Path(csv_path)
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8", newline="") as fh:
        for raw in csv.DictReader(fh):
            row = {}
            for k, v in raw.items():
                if v == "" or v is None:
                    row[k] = None
                elif k in ("date", "collection_status"):
                    row[k] = v
                else:
                    try:
                        row[k] = float(v) if "." in v else int(v)
                    except ValueError:
                        row[k] = v
            rows.append(row)
    return rows

@router.get("/ops/dashboard.html", include_in_schema=False)
def ops_dashboard_html(key: str = "", lang: str = "ja") -> Response:
    """組み立て済みのダッシュボード HTML を返す。

    **クラウド側に生成器を持たせない。** リポジトリは非公開なので、
    ルーティンから clone するには GitHub の接続が要る。ここで組み立てて
    返せば、ルーティンは「取ってきて公開する」だけで済み、生成の作法が
    サーバ側の1か所に留まる（試験もリポジトリ側にある）。

    毎朝1〜2回しか叩かれないので、都度組み立てで足りる。
    """
    settings = get_settings()
    if not settings.ops_api_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not secrets.compare_digest(key, settings.ops_api_key):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if lang not in ("ja", "en"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="lang は ja か en")

    from app.dashboard_publish import build_payload
    payload = build_payload(settings)
    if not payload["kpi"]:
        # **空の殻を配らない。** 推移を見せる画面なので、1日も無い状態で
        # 組み立てると軸も凡例も意味を失う。理由を返して止める。
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="KPI がまだ1日ぶんも無い")

    from app.dashboard_build import render_dashboard
    html = render_dashboard(payload, lang)
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-store"})
