"""日次の価格収集ジョブ。

**見るのは monitored_routes の active 行だけで、watches は見ない。**
同じ出発地・到着地・出発日を何人が登録していても収集は1回で済む（2.4）。
API コストがユーザー数ではなく「異なるルート数」に比例するのは、ここで
watches を辿らないことによる。

fast_flights への依存はここには無い（provider 越し）。差し替え時に
このファイルを触らずに済むようにするため。
"""
import logging
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from app import clock
from app.collect.provider import PriceProvider, ProviderError, Quote
from app.config import LCC_CARRIERS, Settings, get_settings
from app.db import repository

# collection_logs.errors に積むエラーの最大件数。全件入れると、収集が
# 全滅した日に巨大な JSON がログ行に載る。
_MAX_LOGGED_ERRORS = 20

logger = logging.getLogger(__name__)

# 再検索の前に待つ。試験では差し替えて待たない。
_sleep = time.sleep


@dataclass(frozen=True)
class Query:
    """1ルートに対して発行する検索1回分。"""
    stops: int
    airlines: Optional[tuple[str, ...]] = None   # None = 航空会社を指定しない

    @property
    def label(self) -> str:
        if self.airlines:
            return f"stops={self.stops} airlines={','.join(self.airlines)}"
        return f"stops={self.stops}"


def plan_queries(settings: Optional[Settings] = None) -> list[Query]:
    """1ルートに対して発行する検索の一覧。

    通常検索（航空会社無指定）に加えて、LCC を airlines フィルタで明示指定した
    検索を足す。既存 flight-price-monitor の README に記録された未解決バグへの
    対応で、ZIPAIR は無指定検索だとほぼ結果に出ないが、明示指定すると公式サイト
    とほぼ一致する運賃（差2%程度）が返ってくる。無指定検索だけだと $1,600 台を
    「最安」として掴む一方、実際には $490 台が買えていた。

    ネットワークを叩かずに件数を数えられるよう、収集本体から切り出してある
    （CLI の --dry-run が使う）。
    """
    settings = settings or get_settings()
    queries = [Query(stops=s) for s in range(settings.max_stops + 1)]
    for code in LCC_CARRIERS.values():
        for s in range(settings.lcc_search_max_stops + 1):
            queries.append(Query(stops=s, airlines=(code,)))
    return queries


def collect_route(provider: PriceProvider, origin: str, destination: str,
                  departure_date: str, check_date: date, fetched_at: str,
                  settings: Optional[Settings] = None,
                  return_date: str = ""
                  ) -> tuple[list[repository.Snapshot], list[tuple[int, str]],
                             list[int]]:
    """1ルート・1出発日を収集する。例外は投げず、失敗は errors に載せて返す。

    `return_date` が空文字なら片道、日付なら往復。**往復も1ルート1クエリ**
    なので、片道と同じだけしか収集枠を食わない。

    戻り値: (snapshots, [(stops, エラーメッセージ), ...], [便が0件だった stops])

    **3つ目が要る理由。** 検索が通って0件だったことは、失敗とは別の情報で、
    「その区間に便が無い」ことを示す唯一の証拠になる。捨てると、運航されて
    いない区間を永久に検出できない（2026-08-30 の報告）。
    """
    settings = settings or get_settings()
    dep = date.fromisoformat(departure_date)
    ret = date.fromisoformat(return_date) if return_date else None
    snapshots: list[repository.Snapshot] = []
    errors: list[tuple[int, str]] = []
    # (経由回数, 0件の理由)。理由は取得側が残していれば使う。
    empty: list[tuple[int, str]] = []

    for query in plan_queries(settings):
        try:
            quotes = provider.fetch(origin, destination, dep, query.stops,
                                    airlines=query.airlines,
                                    return_date=ret)
        except ProviderError as exc:
            errors.append((query.stops, f"{query.label}: {exc}"))
            continue
        except Exception as exc:  # provider の想定外の失敗で日次収集を止めない
            errors.append((
                query.stops,
                f"{query.label}: unexpected "
                + "".join(traceback.format_exception_only(exc)).strip(),
            ))
            continue

        # 検索ごとに独立して安い順 top_n を取る。全検索をまとめてから
        # 切ると、LCC 明示検索の結果が通常検索の安値に押し出されて消える
        # ことがある — 取りこぼしを拾うための検索なのに本末転倒になる。
        if not quotes:
            # **例外ではない。** 検索は通っていて、便が無かった。
            empty.append((query.stops,
                          getattr(provider, "last_empty_reason", None) or "unknown"))
            continue

        for quote in _cheapest_distinct(quotes, settings.top_n_itineraries):
            snapshots.append(repository.Snapshot(
                check_date=check_date.isoformat(),
                departure_date=departure_date,
                return_date=return_date,
                origin=origin,
                destination=destination,
                stops=quote.stops,
                via_airport=quote.via_airport,
                airline=quote.airline,
                price_usd=quote.price_usd,
                source="real",     # シミュレーション値は絶対にここから入れない
                fetched_at=fetched_at,
                # **'' に寄せる。** UNIQUE キーの一部で、NULL のままだと
                # SQLite が NULL != NULL と評価して重複排除が壊れる。
                departure_time=quote.departure_time or "",
                arrival_time=quote.arrival_time,
                air_minutes=quote.air_minutes,
                total_minutes=quote.total_minutes,
                plane_types=quote.plane_types,
                carbon_g=quote.carbon_g,
            ))

    # **1件でも取れていれば「便が無い」ではない。**
    # 検索は経由回数ごと・LCC 明示ぶんと複数回投げるので、そのうち1本が
    # 0件でも珍しくない。取りこぼしを拾うための検索が空だっただけの話で、
    # 区間に便が無いこととは関係がない。全検索を通して1件も無いときだけ、
    # 「便が無い」証拠として残す。
    if snapshots:
        empty = []
    return snapshots, errors, empty


def _cheapest_distinct(quotes: list[Quote], top_n: int) -> list[Quote]:
    """(経由回数, 経由地, 航空会社, 出発時刻) ごとに最安を1件だけ残し、
    安い順に top_n 件返す。

    snapshots の UNIQUE キーはこの3項目（+ 日付・ルート）なので、同一キーの
    見積りが複数あると upsert で潰し合う。実測でも「Korean Air, ICN 経由」が
    出発時刻違いで複数返ってきた。

    ここで先に畳まないと2つ困る。

    1. ON CONFLICT DO UPDATE は後勝ちなので、安い順に insert すると **高い方**
       が最終的に残る。最安値を追うアプリとしては逆。
    2. top_n の枠を重複が食い潰し、実際に保存される航空会社が top_n より
       少なくなる（実測で 13件 insert して 11件しか残らなかった）。

    **出発時刻まで含めるのは 2026-09-08 から。** それまで3項目で畳んでいた
    ため、同じ社・同じ経由地の別便（SAN→HND の United+ANA・LAX 経由は
    06:15 と 17:00 の2本）が1枠を奪い合い、安いほうだけが残っていた。
    その結果、8/23〜8/27 に $688 で出ていた便は、それ以前の $588 とは
    別の便だった — **1本の線に見えて、実は別々の便のつぎはぎだった。**
    """
    best: dict[tuple, Quote] = {}
    for quote in quotes:
        key = (quote.stops, quote.via_airport, quote.airline,
               quote.departure_time or "")
        current = best.get(key)
        if current is None or quote.price_usd < current.price_usd:
            best[key] = quote
    return sorted(best.values(), key=lambda q: q.price_usd)[:top_n]


def cadence_interval(days_before: int, cadence) -> int:
    """出発まで days_before 日のルートを、何日に1回引くか。"""
    for upper, every in cadence:
        if days_before <= upper:
            return max(int(every), 1)
    return max(int(cadence[-1][1]), 1)


def is_due(route_id: int, departure_date: str, check_date: date, cadence) -> bool:
    """今日引く番か。

    **ルート番号と日付で振り分ける。** 同じ間隔のルートが毎日ほぼ同じ本数ずつ
    引かれる（全部が同じ日に集まらない）。ルート番号は登録順なので、まとめて
    登録した出発日の並びが別々の日に回る。
    """
    days_before = (date.fromisoformat(departure_date) - check_date).days
    every = cadence_interval(days_before, cadence)
    return every <= 1 or (route_id + check_date.toordinal()) % every == 0


def collect_once(provider: Optional[PriceProvider] = None,
                 check_date: Optional[date] = None) -> dict:
    """active な全ルートを1巡する。**例外を投げない。**

    1ルートの失敗で残り全部の収集を落とさないため、失敗は fetch_errors に
    記録して次のルートへ進む。結果は collection_logs にも残す。
    """
    settings = get_settings()
    if provider is None:
        # fast_flights の import をここに閉じ込める。フェイク provider を使う
        # テストでは fast_flights が無くてもこのモジュールを import できる。
        from app.collect.fast_flights_provider import FastFlightsProvider
        provider = FastFlightsProvider(settings)

    check_date = check_date or clock.today()
    fetched_at = datetime.now().isoformat(timespec="seconds")
    started = time.monotonic()

    routes = repository.active_routes()
    # 調査用ルートは、出発が遠いほど間を空けて引く（config.research_cadence）。
    skipped_by_cadence = 0
    if settings.research_cadence_enabled:
        research = repository.research_only_route_ids(check_date.isoformat())
        due = [r for r in routes
               if r.id not in research
               or is_due(r.id, r.departure_date, check_date, settings.research_cadence)]
        skipped_by_cadence = len(routes) - len(due)
        routes = due
        if skipped_by_cadence:
            logger.info("取得頻度で今日は見送り: 調査用 %d 本（引くのは %d 本）",
                        skipped_by_cadence, len(routes))
    queries_per_route = len(plan_queries(settings))
    rows_written = 0
    error_messages: list[str] = []

    # **以前は便が取れていたルートの0件は、すぐには記録しない。**
    #
    # 2026-10-01 ごろから、収集の最中にだけ0件が増えた（9月 1〜5% → 10月
    # 15〜23%）。羽田→ロサンゼルスのような路線まで0件になり、約8割は翌日に
    # 戻る。同じ検索を収集の時間帯以外に本番サーバから流すと便が取れた
    # （10/12 確認）。収集の後半ほど多く、短時間に大量に引くと一部が空で
    # 返されている見立て。**取得失敗には出ないので、静かにデータが欠けていた。**
    #
    # 本ループの後に間を置いて1回だけ引き直し、それでも0件のものだけ記録する。
    # 一度も取れたことの無いルート（予約受付前など）は対象外 — 0件が正常で、
    # 引き直しても増えるのは無駄な検索だけ。
    retry_enabled = settings.empty_retry_enabled
    established = (repository.routes_with_history(check_date.isoformat())
                   if retry_enabled else set())
    deferred = []

    def _log_empties(route, empty):
        for stops, reason in empty:
            repository.log_empty_search(
                check_date=check_date.isoformat(),
                origin=route.origin, destination=route.destination,
                departure_date=route.departure_date,
                return_date=route.return_date, stops=stops, reason=reason,
            )

    def _log_errors(route, errors):
        for stops, message in errors:
            repository.log_fetch_error(
                check_date=check_date.isoformat(),
                origin=route.origin, destination=route.destination,
                departure_date=route.departure_date,
                return_date=route.return_date,
                stops=stops, via_airport=None, error_message=message,
            )
            error_messages.append(
                f"{route.origin}->{route.destination} "
                f"{route.departure_date} {message}"
            )

    for route in routes:
        snapshots, errors, empty = collect_route(
            provider, route.origin, route.destination, route.departure_date,
            check_date, fetched_at, settings, route.return_date,
        )
        rows_written += repository.upsert_snapshots(snapshots)

        key = (route.origin, route.destination, route.departure_date,
               route.return_date)
        if empty and key in established:
            deferred.append((route, empty))
        else:
            _log_empties(route, empty)
        _log_errors(route, errors)

        # 全クエリが失敗したルートは「収集済み」にしない。次回の巡回で
        # 未収集として扱えるようにしておく。
        if len(errors) < queries_per_route:
            repository.mark_route_collected(route.id)

    # ── 再検索 ───────────────────────────────────────────────
    recovered = 0
    first_pass_reasons: dict[str, int] = {}
    for _route, empty in deferred:
        for _stops, reason in empty:
            first_pass_reasons[reason] = first_pass_reasons.get(reason, 0) + 1
    if deferred:
        _sleep(settings.empty_retry_delay_seconds)
        for route, _first in deferred:
            snapshots, errors, empty = collect_route(
                provider, route.origin, route.destination, route.departure_date,
                check_date, fetched_at, settings, route.return_date,
            )
            written = repository.upsert_snapshots(snapshots)
            rows_written += written
            if snapshots:
                recovered += 1
            _log_empties(route, empty)   # 引き直しても0件のものだけ残る
            _log_errors(route, errors)
        logger.info(
            "0件の再検索: %d 本中 %d 本が回復（本ループの0件の理由: %s）",
            len(deferred), recovered, first_pass_reasons)

    duration_ms = int((time.monotonic() - started) * 1000)
    # collection_logs.status の CHECK 制約に合わせる
    # （'success' | 'partial' | 'failed'）。
    if not error_messages:
        status = "success"
    elif rows_written:
        status = "partial"
    else:
        status = "failed"

    repository.insert_collection_log(
        check_date=check_date.isoformat(),
        status=status,
        source=provider.name,
        routes_checked=len(routes),
        records_saved=rows_written,
        errors=error_messages[:_MAX_LOGGED_ERRORS] or None,
        duration_ms=duration_ms,
    )

    return {
        "check_date": check_date.isoformat(),
        "status": status,
        "routes_checked": len(routes),
        "rows_written": rows_written,
        "errors": len(error_messages),
        "duration_ms": duration_ms,
        # 0件の再検索（2026-10-12 から）。retried が多いほど収集中の空振りが多い。
        "empty_retried": len(deferred),
        "empty_recovered": recovered,
        "empty_first_pass_reasons": first_pass_reasons,
        "skipped_by_cadence": skipped_by_cadence,
    }
