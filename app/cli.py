"""管理コマンド。

    python -m app.cli init-db
    python -m app.cli add-route HND SAN 2026-12-20
    python -m app.cli routes
    python -m app.cli status
    python -m app.cli collect --dry-run
    python -m app.cli collect --once
    python -m app.cli predict HND SAN 2026-10-15
    python -m app.cli predict --log
    python -m app.cli run-daily
    python -m app.cli dataset
    python -m app.cli export --completed-only --out prices.csv
"""
import argparse
import sys
from datetime import date, timedelta

from app.config import get_settings
from app.db import connection, repository
from app.routing import expand_routes


def cmd_init_db(_args) -> int:
    path = connection.init_db()
    tables = connection.table_names()
    print(f"✅ データベース初期化完了: {path}")
    print(f"   テーブル ({len(tables)}): {', '.join(tables)}")
    return 0


def cmd_add_route(args) -> int:
    """手動でモニターを1件登録する（Phase 2 の収集動作確認用）。"""
    try:
        date.fromisoformat(args.departure_date)
    except ValueError:
        print(f"❌ 出発日が YYYY-MM-DD 形式ではありません: {args.departure_date}",
              file=sys.stderr)
        return 1

    settings = get_settings()
    pairs = expand_routes(args.origin.upper(), args.destination.upper())

    # 上限チェック。到達時は黙って収集を落とさず、明示的に拒否する。
    if repository.count_active_routes() + len(pairs) > settings.max_active_routes:
        print(f"❌ 監視中ルート数が上限 ({settings.max_active_routes}) に達しています",
              file=sys.stderr)
        return 1

    route_ids = [
        repository.upsert_monitored_route(o, d, args.departure_date)
        for o, d in pairs
    ]
    watch = repository.create_watch(
        origin=args.origin.upper(),
        destination=args.destination.upper(),
        departure_date=args.departure_date,
        route_ids=route_ids,
    )

    print(f"✅ モニターを登録しました  /w/{watch.token}")
    print(f"   有効期限: {watch.expires_at}")
    print(f"   収集対象ルート ({len(pairs)}):")
    for o, d in pairs:
        note = "  ← 近接空港ルール" if (o, d) != (watch.origin, watch.destination) else ""
        print(f"     {o} -> {d}{note}")
    return 0


def cmd_routes(_args) -> int:
    routes = repository.active_routes()
    if not routes:
        print("(監視中のルートはありません)")
        return 0
    print(f"監視中ルート: {len(routes)} 件")
    for r in routes:
        print(f"  [{r.id}] {r.origin} -> {r.destination}  出発 {r.departure_date}")
    return 0


def cmd_status(_args) -> int:
    from app import clock

    settings = get_settings()
    print(f"DB              : {settings.db_path}")
    print(f"今日 ({settings.timezone}): {clock.today()}")
    print(f"監視中ルート    : {repository.count_active_routes()} / {settings.max_active_routes}")
    print(f"最新の収集日    : {repository.latest_check_date() or '(なし)'}")
    log = repository.latest_collection_log()
    print(f"直近の収集ジョブ: {log['status'] + ' @ ' + log['created_at'] if log else '(なし)'}")
    print(f"原油価格の係数  : {settings.oil_coefficient}  (0 = 無効)")
    schedule = (f"{settings.daily_run_hour:02d}:{settings.daily_run_minute:02d} "
                f"{settings.timezone}" if settings.scheduler_enabled else "無効")
    print(f"日次スケジュール: {schedule}")
    return 0


def cmd_sync_routes(args) -> int:
    """登録済みモニターを、いまの近接空港ルールで展開し直す。

    expand_routes は登録時にしか走らないので、NEARBY_AIRPORTS を変えても
    既存モニターには反映されない。ルールを広げたときはこれを流す。
    """
    settings = get_settings()
    watches = repository.all_watches()
    if not watches:
        print("(モニターがありません)")
        return 0

    planned: list[tuple] = []
    for watch in watches:
        for origin, destination in expand_routes(
                watch.origin, watch.destination,
                nearby_airports=watch.nearby_airports):
            planned.append((watch, origin, destination))

    # 増える分だけを数える（既存ルートは upsert で増えない）。
    existing = {
        (r.origin, r.destination, r.departure_date)
        for r in repository.active_routes()
    }
    new_routes = {
        (o, d, w.departure_date) for w, o, d in planned
    } - existing

    print(f"モニター {len(watches)} 件 / 新たに増える収集ルート {len(new_routes)} 件")
    for origin, destination, departure in sorted(new_routes):
        print(f"  + {origin} -> {destination}  出発 {departure}")

    if args.dry_run:
        print("\n--dry-run のため変更していません。")
        return 0

    if repository.count_active_routes() + len(new_routes) > settings.max_active_routes:
        print(f"❌ 監視中ルート数が上限 ({settings.max_active_routes}) を超えます",
              file=sys.stderr)
        return 1

    from app.watches import sync_watch_routes

    linked = unlinked = 0
    refreshed = []
    for watch in watches:
        result = sync_watch_routes(watch)
        linked += result.linked
        unlinked += result.unlinked
        # 失効規則（collect_until_departure 等）の変更も既存モニターへ反映する。
        # last_accessed_at は触らない。
        new_expiry = repository.refresh_expiry(watch.token)
        if new_expiry and new_expiry != watch.expires_at:
            refreshed.append((watch, watch.expires_at, new_expiry))

    print(f"\n✅ 紐付けを {linked} 件追加 / {unlinked} 件解除しました "
          f"（監視中ルート {repository.count_active_routes()} 件）")
    for watch, old, new in refreshed:
        print(f"   有効期限 {watch.origin}->{watch.destination} "
              f"出発{watch.departure_date}: {old} → {new}")
    return 0


def cmd_health(args) -> int:
    """収集の健全性。**新しく壊れたものだけ**を目立たせる。

    恒常的に失敗する組み合わせが1つでもあると `status` は毎日 partial に
    なり、警告として使えなくなる。失敗の記録は抑制せず、代わりに
    「今日はじめて出たか」で分けて見せる。
    """
    from datetime import timedelta

    from app import clock

    today = clock.today()
    since = (today - timedelta(days=args.days)).isoformat()

    trend = repository.collection_trend(since)
    print(f"収集の推移（直近 {args.days} 日）")
    if not trend:
        print("  (記録がありません)")
    for row in trend:
        print(f"  {row['check_date']}  {row['status']:<8} "
              f"{row['routes_checked']:>3}ルート "
              f"{row['records_saved']:>4}件 "
              f"{row['duration_ms'] / 1000:>6.0f}秒 "
              f"エラー{row['errors']}")

    errors = repository.fetch_error_summary(since)
    # **「いつ始まったか」だけでは分類できない。** 直近の収集でも出ているか
    # （last_seen）を見ないと、数日前に1度きり失敗して以後は正常なものまで
    # 「継続中」に並び、直す必要のないものを追いかけることになる。
    #
    # 基準は「最後に収集した日」。エラーの最終日を基準にすると、直近の収集が
    # 無事だったときに、古いエラーが「新規」に化ける。today も使えない
    # （その日の収集がまだ走っていない時間帯に叩くと全件が解消扱いになる）。
    latest = trend[-1]["check_date"] if trend else None
    new = [e for e in errors if e["first_seen"] == latest]
    ongoing = [e for e in errors if e["first_seen"] != latest and e["last_seen"] == latest]
    resolved = [e for e in errors if e["last_seen"] != latest]

    print()
    if new:
        print(f"⚠️  今日はじめて出た失敗: {len(new)} 件  ← 確認が要る")
        for e in new:
            print(f"  {e['origin']}->{e['destination']} stops={e['stops']}")
            print(f"     {e['sample'][:100]}")
    else:
        print("✅ 今日はじめて出た失敗はありません")

    if ongoing:
        print()
        print(f"継続中の失敗: {len(ongoing)} 件（既知。新規ではない）")
        for e in ongoing:
            print(f"  {e['origin']}->{e['destination']} stops={e['stops']}  "
                  f"{e['first_seen']} から 計{e['days_seen']}日")

    if resolved:
        print()
        print(f"解消した失敗: {len(resolved)} 件（直近の収集では出ていない）")
        for e in resolved:
            print(f"  {e['origin']}->{e['destination']} stops={e['stops']}  "
                  f"最後に出たのは {e['last_seen']}")

    cap = repository.count_active_routes()
    limit = get_settings().max_active_routes
    share = cap / limit if limit else 1.0
    mark = "🔴" if share >= 1.0 else ("⚠️ " if share >= get_settings().route_capacity_warn_fraction else "✅")
    print()
    print(f"{mark} ルート枠: {cap} / {limit}  ({share * 100:.0f}%)")

    disabled = repository.disabled_routes()
    if disabled:
        print()
        print(f"収集を停止したルート: {len(disabled)} 件")
        for r in disabled:
            print(f"  {r['origin']}->{r['destination']} {r['departure_date']}  "
                  f"{r['disabled_reason']}（{r['disabled_at'][:10]}）")

    # 新規があれば非ゼロで終わる。cron から叩いて通知に繋げられる。
    # 継続中と解消では終了コードを変えない — 毎日鳴る警告は無視されるため。
    return 1 if new else 0


def cmd_export(args) -> int:
    """蓄積した価格を CSV / JSONL で書き出す。

    学習・分析はサーバの外でやるものなので、SQLite を直接触らせずに
    取り出せる口を用意しておく。
    """
    import csv
    import json as _json

    if args.table == "predictions":
        rows = repository.export_predictions()
    else:
        sources = ("real", "simulated") if args.include_simulated else ("real",)
        rows = repository.export_snapshots(sources, completed_only=args.completed_only)

    if not rows:
        print("(該当するデータがありません)", file=sys.stderr)
        return 0

    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        if args.format == "jsonl":
            for row in rows:
                out.write(_json.dumps(row, ensure_ascii=False) + "\n")
        else:
            writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    finally:
        if args.out:
            out.close()

    if args.out:
        print(f"✅ {len(rows)} 行を {args.out} に書き出しました", file=sys.stderr)
    return 0


def cmd_dataset(_args) -> int:
    """学習データとしての現状を見る。"""
    d = repository.dataset_summary()
    print(f"実測レコード      : {d['real_rows']:,}")
    print(f"観測日数          : {d['observed_days']}  "
          f"({d['first_check'] or '-'} 〜 {d['last_check'] or '-'})")
    print(f"系列 (路線 x 出発日): {d['series']}")
    print(f"  うち完了済み    : {d['completed_series']}  ← 出発日を迎えた系列")
    print(f"予測ログ          : {d['predictions']:,}")
    if not d["completed_series"]:
        print()
        print("完了系列がまだありません。「残り期間の最小値がいくらだったか」は")
        print("出発日を過ぎて初めて確定するため、学習にはこれが要ります。")
    return 0


def cmd_kpi(args) -> int:
    from app import kpi

    if args.show:
        path = get_settings().kpi_csv_path
        if not path.exists():
            print(f"まだ記録がない: {path}")
            return 0
        print(path.read_text(encoding="utf-8").rstrip())
        return 0

    row = kpi.collect_kpi()
    if args.dry_run:
        for k, v in row.items():
            print(f"  {k:<28} {v}")
        return 0

    result = kpi.record_daily()
    print(f"記録: {result['csv_path']} ({result['row']['date']})")
    print(f"S3  : {result['s3_uri'] or '未設定（KPI_S3_BUCKET が空）'}")
    return 0


def cmd_run_daily(_args) -> int:
    """日次ジョブを1回まわす。

    Web プロセスを常駐させない運用（cron / launchd / EventBridge）では
    こちらを叩く。**失敗しても終了コードは 0 のまま**にしてある。
    無人実行でスケジューラ側にエラー通知を積み上げても誰も見ないし、
    実際の状態は collection_logs に残るため。
    """
    from app.jobs.daily import run_daily
    from app.logging_setup import configure_logging

    # cron / launchd から無人で回る経路。ログが出ないと失敗に気づけない。
    configure_logging()
    summary = run_daily()
    life = summary["lifecycle"] or {}
    collection = summary["collection"] or {}

    print(f"日次ジョブ完了: {summary['date']}  ({summary['duration_ms'] / 1000:.1f} 秒)")
    print(f"  失効した監視  : {life.get('expired_watches', '-')}")
    print(f"  停止したルート: 参照なし {life.get('orphan_routes', '-')} / "
          f"出発済み {life.get('departed_routes', '-')}")
    print(f"  収集          : {collection.get('status', '-')}  "
          f"{collection.get('rows_written', 0)} 件保存 / "
          f"{collection.get('errors', 0)} 件エラー")
    print(f"  予測の記録    : {summary['predictions_logged']} 行")
    if summary["failed_steps"]:
        print(f"  ⚠️ 失敗した工程: {', '.join(summary['failed_steps'])}")
    return 0


def cmd_collect(args) -> int:
    from app.collect.collector import plan_queries

    settings = get_settings()
    routes = repository.active_routes()
    queries = plan_queries(settings)

    if not routes:
        print("(監視中のルートはありません)")
        return 0

    if args.dry_run:
        total = len(routes) * len(queries)
        avg_delay = sum(settings.request_delay_range) / 2
        print(f"収集対象ルート: {len(routes)} 件")
        print(f"1ルートあたりの検索: {len(queries)} 回")
        for q in queries:
            print(f"    {q.label}")
        print(f"合計リクエスト数: {total} 回")
        print(f"推定所要時間    : 約 {total * avg_delay / 60:.0f} 分"
              f"（平均 {avg_delay:.1f} 秒間隔）")
        print("\n--dry-run のためネットワークアクセスは行いません。")
        return 0

    print(f"収集を開始します（{len(routes)} ルート x {len(queries)} 検索 "
          f"= {len(routes) * len(queries)} リクエスト）")
    summary = collector_collect_once()
    print(f"\n結果: {summary['status']}")
    print(f"  ルート数    : {summary['routes_checked']}")
    print(f"  保存レコード: {summary['rows_written']}")
    print(f"  エラー      : {summary['errors']}  (詳細は fetch_errors テーブル)")
    print(f"  所要時間    : {summary['duration_ms'] / 1000:.1f} 秒")
    # 収集自体が失敗しても異常終了はしない（Phase 5 のスケジューラから
    # 呼ばれたときにジョブを落とさないため）。状態は collection_logs にある。
    return 0


def collector_collect_once():
    """収集本体の遅延 import。fast_flights の import をここまで遅らせる。"""
    from app.collect.collector import collect_once
    return collect_once()


def cmd_add_research_monitors(args) -> int:
    """価格曲線を調べるためのモニターを、出発日を散らして一括登録する。

        python -m app.cli add-research-monitors \
            --routes SAN-HND,HND-SAN,SAN-NRT,NRT-SAN \
            --from-days 90 --to-days 180 --count 30

    **なぜ要るか。** 取得元から遡れる履歴は約61日しかなく、1系列では
    出発までの全区間を見られない（打ち切り）。出発日の違う系列を重ねて
    形を復元したが、それでは「同じ便が実際に下がる時期があるか」に
    答えられない。**出発日まで追い切った系列**が必要で、それは今から
    仕込むしか作れない。

    `purpose='research'` を立てる。KPI の需要・定着の列から外すため。
    区別しないと「作られたきり開かれていないモニター」の件数が我々の分で
    埋まり、利用者が使っているかを測れなくなる。

    出発日は等間隔に散らす。**同じ日付を全ルートで共有する** —
    SAN→HND と HND→SAN を同じ出発日で比べられるようにするため
    （日付ごとの効果を打ち消せる）。

    近接空港ルールは使わない。指定した区間だけを、狙って観測する。

    `--stay-nights` を渡すと**往復**を登録する（例 `--stay-nights 7,10,14`）。
    滞在日数は往復にしかない次元で、片道の曲線をそのまま当てはめてよいかを
    決める鍵になる。渡さなければ従来どおり片道。

    **滞在日数を複数にするのは、曜日の効果と長さの効果を分けるため。**
    7泊と14泊は復路の曜日が出発と同じになる（7の倍数）ので、長さだけが
    変わる。10泊はそれを崩すので、復路の曜日が効くかどうかが見える。
    """
    pairs = []
    for token in args.routes.split(","):
        token = token.strip().upper()
        if "-" not in token:
            print(f"❌ ルートの書き方が違います: {token}（例 SAN-HND）", file=sys.stderr)
            return 1
        o, d = token.split("-", 1)
        pairs.append((o, d))

    nights: list[int] = []
    if args.stay_nights:
        try:
            nights = sorted({int(n) for n in args.stay_nights.split(",") if n.strip()})
        except ValueError:
            print("❌ --stay-nights は整数のカンマ区切り（例 7,10,14）", file=sys.stderr)
            return 1
        if any(n < 1 for n in nights):
            print("❌ 滞在日数は1以上", file=sys.stderr)
            return 1

    span = args.to_days - args.from_days
    if span <= 0 or args.count < 1:
        print("❌ --to-days は --from-days より後、--count は1以上", file=sys.stderr)
        return 1

    today = date.today()
    # 等間隔。count=1 のときはゼロ除算にならないよう分母を守る。
    step = span / max(args.count - 1, 1)
    dates = [(today + timedelta(days=int(round(args.from_days + i * step)))).isoformat()
             for i in range(args.count)]
    dates = sorted(set(dates))

    settings = get_settings()
    total = len(pairs) * len(dates) * max(len(nights), 1)
    active = repository.count_active_routes()
    if nights:
        print(f"区間 {len(pairs)} × 出発日 {len(dates)} × 滞在 {len(nights)} 通り"
              f"（{','.join(str(n) for n in nights)}泊）= {total} ルート")
    else:
        print(f"区間 {len(pairs)} × 出発日 {len(dates)} = {total} ルート（片道）")
    print(f"出発日: {dates[0]} 〜 {dates[-1]}（{args.from_days}〜{args.to_days} 日後）")
    print(f"現在の稼働ルート: {active} / {settings.max_active_routes}"
          f" → 追加後 {active + total}")

    if active + total > settings.max_active_routes:
        print(f"❌ 上限 {settings.max_active_routes} を超えます", file=sys.stderr)
        return 1

    if args.dry_run:
        for d in dates[:6]:
            if nights:
                rets = " ".join(
                    (date.fromisoformat(d) + timedelta(days=n)).isoformat()
                    for n in nights)
                print(f"    {d} → 復路 {rets}")
            else:
                print(f"    {d}  " + " / ".join(f"{o}-{dd}" for o, dd in pairs))
        if len(dates) > 6:
            print(f"    … 他 {len(dates) - 6} 日付")
        print("\n--dry-run のため登録しません。")
        return 0

    created = 0
    for departure in dates:
        # 片道は return_date='' の1通り。往復は滞在日数のぶんだけ。
        returns = ([(date.fromisoformat(departure) + timedelta(days=n)).isoformat()
                    for n in nights] if nights else [""])
        for ret in returns:
            for origin, destination in pairs:
                route_id = repository.upsert_monitored_route(
                    origin, destination, departure, ret)
                repository.create_watch(
                    origin=origin, destination=destination,
                    departure_date=departure, route_ids=[route_id],
                    nearby_airports=False, purpose="research",
                    return_date=ret)
                created += 1
    print(f"\n登録しました: {created} 件（purpose=research）")
    print("KPI の需要・定着の列には数えません（watches_research に件数が出ます）")
    return 0


def cmd_backfill_history(args) -> int:
    """取得元が持っている過去の価格を、まだ取り込んでいないルートに入れる。

    **1ルート1回でよい。** 翌日以降の1点は snapshots として自分で観測して
    いるので、取り直しても同じものが増えるだけ。だから対象は
    `routes_without_history()` に限る。

    途中で止まっても、次に流せば残りから再開する（取り込み済みは除外される）。
    """
    summary = repository.price_history_summary()
    pending = repository.routes_without_history()
    print(f"取り込み済み: {summary['routes'] or 0} ルート / {summary['points'] or 0} 点"
          + (f"（{summary['since']}〜{summary['until']}）" if summary["points"] else ""))
    print(f"未取り込み  : {len(pending)} ルート")

    if not pending:
        print("\n対象はありません。")
        return 0

    target = pending[:args.limit] if args.limit else pending
    settings = get_settings()
    avg = sum(settings.request_delay_range) / 2
    print(f"今回の対象  : {len(target)} ルート（推定 約 {len(target) * avg / 60:.0f} 分）")

    if args.dry_run:
        for r in target[:20]:
            _ret = r.get("return_date") or ""
            print(f"    {r['origin']}-{r['destination']} {r['departure_date']}"
                  + (f"/{_ret}" if _ret else ""))
        if len(target) > 20:
            print(f"    … 他 {len(target) - 20} 件")
        print("\n--dry-run のためネットワークアクセスは行いません。")
        return 0

    from app.collect.fast_flights_provider import FastFlightsProvider
    provider = FastFlightsProvider(settings)

    saved = empty = 0
    for i, r in enumerate(target, 1):
        ret = r.get("return_date") or ""
        head = (f"  [{i}/{len(target)}] {r['origin']}-{r['destination']} "
                f"{r['departure_date']}" + (f"/{ret}" if ret else ""))
        points = provider.fetch_history(
            r["origin"], r["destination"], date.fromisoformat(r["departure_date"]),
            return_date=date.fromisoformat(ret) if ret else None)
        if not points:
            # **履歴が無いこと自体は失敗ではない。** 新規開設路線や在庫の
            # 薄い区間では取得元も持っていない。
            empty += 1
            print(f"{head}  履歴なし")
            continue
        saved += repository.save_price_history(
            r["origin"], r["destination"], r["departure_date"],
            [(p.observed_date.isoformat(), p.price_usd) for p in points],
            return_date=ret)
        prices = [p.price_usd for p in points]
        print(f"{head}  {len(points)} 点  "
              f"{points[0].observed_date}〜{points[-1].observed_date}  "
              f"US${min(prices):.0f}–{max(prices):.0f}")

    print(f"\n保存 {saved} 点 / 履歴なし {empty} ルート")
    return 0


_VERDICT_LABELS = {
    "buy_now": "今買う",
    "wait": "待つ",
    "watch": "様子見",
}


def cmd_predict(args) -> int:
    from app.predict.engine import log_predictions, recommend

    if args.origin:
        if not (args.destination and args.departure_date):
            print("❌ origin / destination / departure_date は3つセットで指定してください",
                  file=sys.stderr)
            return 1
        targets = [(args.origin.upper(), args.destination.upper(),
                    args.departure_date)]
    else:
        targets = [(r.origin, r.destination, r.departure_date)
                   for r in repository.active_routes()]

    if not targets:
        print("(監視中のルートはありません)")
        return 0

    logged = 0
    for origin, destination, departure_date in targets:
        result = recommend(origin, destination, departure_date)
        _print_recommendation(origin, destination, departure_date, result)
        if args.log and result:
            logged += log_predictions(origin, destination, departure_date)

    if args.log:
        print(f"\nprediction_log に {logged} 行を記録しました。")
    return 0


def _print_recommendation(origin, destination, departure_date, result) -> None:
    header = f"{origin} -> {destination}  出発 {departure_date}"
    print(f"\n{header}")
    print("-" * len(header))
    if result is None:
        print("  実測がまだありません（先に collect を実行してください）")
        return

    verdict = _VERDICT_LABELS.get(result["verdict"], result["verdict"])
    print(f"  判定        : {verdict}   （実測 {result['observed_days']} 日分）")

    current = result["current_best"]
    if current:
        print(f"  今の最安    : ${current['price_usd']:,.0f}  "
              f"{current['airline']}  {_route_note(current)}"
              f"  ({current['check_date']} 時点)")

    lowest = result["lowest_observed"]
    if lowest and lowest["check_date"] != (current or {}).get("check_date"):
        # 過去の記録であって、今買える価格ではない。
        print(f"  過去最安    : ${lowest['price_usd']:,.0f}  "
              f"{lowest['airline']}  ({lowest['check_date']} の記録)")

    predicted = result["predicted_min"]
    if predicted:
        print(f"  予測の底値  : ${predicted['price_usd']:,.0f}  "
              f"(${predicted['lower_usd']:,.0f} 〜 ${predicted['upper_usd']:,.0f})  "
              f"{predicted['airline']}  {predicted['check_date']} ごろ")

    if result["recommended_check_date"]:
        print(f"  推奨購入日  : {result['recommended_check_date']}")


def _route_note(candidate: dict) -> str:
    if candidate["stops"] == 0:
        return "直行"
    return f"{candidate['via_airport']} 経由" if candidate["via_airport"] else "1経由"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="管理コマンド")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="スキーマを適用する（冪等）").set_defaults(func=cmd_init_db)

    p_add = sub.add_parser("add-route", help="モニターを手動登録する")
    p_add.add_argument("origin")
    p_add.add_argument("destination")
    p_add.add_argument("departure_date", help="YYYY-MM-DD")
    p_add.set_defaults(func=cmd_add_route)

    sub.add_parser("routes", help="監視中ルートを一覧する").set_defaults(func=cmd_routes)
    sub.add_parser("status", help="現在の状態を表示する").set_defaults(func=cmd_status)

    p_collect = sub.add_parser("collect", help="価格を収集する")
    p_collect.add_argument("--once", action="store_true",
                           help="1巡だけ収集する（既定の動作）")
    p_collect.add_argument("--dry-run", action="store_true",
                           help="発行するクエリを表示するだけで通信しない")
    p_collect.set_defaults(func=cmd_collect)

    p_predict = sub.add_parser(
        "predict", help="価格予測と買い時判定を表示する（引数なしで全ルート）")
    p_predict.add_argument("origin", nargs="?")
    p_predict.add_argument("destination", nargs="?")
    p_predict.add_argument("departure_date", nargs="?", help="YYYY-MM-DD")
    p_predict.add_argument("--log", action="store_true",
                           help="予測を prediction_log に記録する（精度検証用）")
    p_predict.set_defaults(func=cmd_predict)

    sub.add_parser(
        "run-daily",
        help="日次ジョブ（整理 → 収集 → 予測の記録）を1回まわす",
    ).set_defaults(func=cmd_run_daily)

    p_sync = sub.add_parser(
        "sync-routes",
        help="登録済みモニターを、いまの近接空港ルールで展開し直す")
    p_sync.add_argument("--dry-run", action="store_true",
                        help="増えるルートを表示するだけで変更しない")
    p_sync.set_defaults(func=cmd_sync_routes)

    p_export = sub.add_parser("export", help="蓄積した価格を書き出す")
    p_export.add_argument("--table", choices=["snapshots", "predictions"],
                          default="snapshots")
    p_export.add_argument("--format", choices=["csv", "jsonl"], default="csv")
    p_export.add_argument("--completed-only", action="store_true",
                          help="出発日を迎えた系列だけ（学習に使えるのはこれ）")
    p_export.add_argument("--include-simulated", action="store_true",
                          help="シミュレーション値も含める（既定は実測のみ）")
    p_export.add_argument("--out", help="出力先ファイル（省略時は標準出力）")
    p_export.set_defaults(func=cmd_export)

    sub.add_parser(
        "dataset", help="学習データとしての蓄積状況を見る"
    ).set_defaults(func=cmd_dataset)

    p_kpi = sub.add_parser("kpi", help="日次 KPI を CSV に記録する")
    p_kpi.add_argument("--dry-run", action="store_true",
                       help="書き込まずに当日の値を表示する")
    p_kpi.add_argument("--show", action="store_true", help="記録済みの CSV を表示する")
    p_kpi.set_defaults(func=cmd_kpi)

    p_res = sub.add_parser(
        "add-research-monitors",
        help="価格曲線の調査用モニターを出発日を散らして一括登録する")
    p_res.add_argument("--routes", required=True,
                       help="カンマ区切り（例 SAN-HND,HND-SAN,SAN-NRT,NRT-SAN）")
    p_res.add_argument("--from-days", type=int, default=90, help="最も近い出発日（何日後）")
    p_res.add_argument("--to-days", type=int, default=180, help="最も遠い出発日（何日後）")
    p_res.add_argument("--count", type=int, default=30, help="1区間あたりの出発日の数")
    p_res.add_argument("--stay-nights", default=None,
                       help="往復にする。滞在日数のカンマ区切り（例 7,10,14）。"
                            "省略すると片道")
    p_res.add_argument("--dry-run", action="store_true", help="登録せず内容を表示する")
    p_res.set_defaults(func=cmd_add_research_monitors)

    p_bfh = sub.add_parser(
        "backfill-history",
        help="取得元が持つ過去の価格を取り込む（未取り込みのルートのみ）")
    p_bfh.add_argument("--limit", type=int,
                       help="今回処理するルート数の上限（分割して流すとき）")
    p_bfh.add_argument("--dry-run", action="store_true",
                       help="対象を表示するだけで取得しない")
    p_bfh.set_defaults(func=cmd_backfill_history)

    p_health = sub.add_parser(
        "health", help="収集の健全性。新しく壊れたものがあれば終了コード1")
    p_health.add_argument("--days", type=int, default=14)
    p_health.set_defaults(func=cmd_health)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
