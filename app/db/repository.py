"""データアクセス層。**SQL はこのファイルにしか書かない。**

PI-27-flightTrend は SQL を Express のルートハンドラに直書きしていたため、
ストレージを差し替える余地が実質なくなっていた。同じ轍を踏まないよう、
ハンドラ・収集ジョブ・予測エンジンはすべてここ越しに DB を触る。
"""
import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from app.config import get_settings
from app.db.connection import get_connection

# トークンの長さ（バイト）。token_urlsafe(16) で 128bit、22文字の URL-safe 文字列。
# 連番 ID は不可（推測可能なため）。
_TOKEN_BYTES = 16

# 「指定なし（既定値を使う）」と「明示的な None（上限なし）」を区別する番兵。
# None を既定にすると、上限を外したい呼び出しと区別できない。
_UNSET: Any = object()


def new_token() -> str:
    """推測不可能なモニタートークンを発行する。

    呼び出し側にトークンを作らせない（弱いトークンを渡される事故を防ぐ）。
    """
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def expires_at_for(departure_date: str, as_of: Optional[datetime] = None) -> str:
    """モニターの失効日。

    `collect_until_departure` が真なら「出発日 + 猶予」だけで決める。放置期限で
    先に切れると、系列が出発日に届かず**途中で切れた観測列**になる。途中で
    切れた系列は「残り期間の最小値がいくらだったか」が確定しないので、
    価格曲線の推定にも学習にも使えない。

    偽にすると仕様 2.4 当初の「放置期限と出発日猶予の早い方」に戻る。
    """
    settings = get_settings()
    as_of = as_of or datetime.now()

    departure_limit = (date.fromisoformat(departure_date)
                       + timedelta(days=settings.watch_post_departure_grace_days))
    if settings.collect_until_departure:
        return departure_limit.isoformat()

    idle_limit = (as_of + timedelta(days=settings.watch_idle_expiry_days)).date()
    return min(idle_limit, departure_limit).isoformat()


# ============================================================
# データクラス
# ============================================================

@dataclass(frozen=True)
class Watch:
    id: int
    token: str
    origin: str
    destination: str
    departure_date: str
    airline_filter: list[str]
    max_total_minutes: Optional[int]
    nearby_airports: bool
    notify_email: Optional[str]
    created_at: str
    last_accessed_at: str
    expires_at: str
    # 'user' か 'research'。KPI の需要・定着の列は 'user' だけを数える。
    # **既定値つきなので末尾に置く。** 途中に入れると、既定値の無い
    # フィールドがその後ろに来て dataclass が組めない。
    purpose: str = "user"
    return_date: str = ""       # '' が片道
    # 通知の送信済み状態（課題4）。None = まだ何も送っていない。
    notified_low_usd: Optional[float] = None
    notified_rise_at: Optional[str] = None


@dataclass(frozen=True)
class MonitoredRoute:
    id: int
    origin: str
    destination: str
    departure_date: str
    active: bool
    # 自動停止したときの理由。画面で「なぜデータが来ないか」を言うために持つ。
    disabled_reason: Optional[str] = None
    # 停止の種類。'no_flights' | 'fetch_failed' | None
    # **文言の出し分けはこちらで行う。** 理由文は人が読むためのもので、
    # 書き換わりうるので条件分岐に使わない。
    disabled_kind: Optional[str] = None
    return_date: str = ""       # '' が片道


@dataclass(frozen=True)
class Snapshot:
    check_date: str
    departure_date: str
    origin: str
    destination: str
    stops: int
    via_airport: Optional[str]
    airline: str
    price_usd: float
    source: str          # 'real' | 'simulated'
    fetched_at: str
    # 便の詳細。取得できなければ None（既存行も None のまま）。
    # **departure_time だけは '' に寄せる。** UNIQUE キーの一部で、SQLite は
    # NULL != NULL と評価するため、NULL のままだと重複排除が黙って壊れる。
    departure_time: str = ""
    arrival_time: Optional[str] = None
    air_minutes: Optional[int] = None
    total_minutes: Optional[int] = None
    plane_types: Optional[str] = None
    carbon_g: Optional[int] = None
    return_date: str = ""       # '' が片道


@dataclass(frozen=True)
class Prediction:
    predicted_on: str
    target_check_date: str
    departure_date: str
    origin: str
    destination: str
    stops: int
    via_airport: Optional[str]
    airline: str
    predicted_usd: float
    lower_usd: float
    upper_usd: float
    observed_points: int
    return_date: str = ""       # '' が片道
    # **便の身元の一部。** 同じ社・同じ経由地で1日に何本も飛ぶので、
    # ここが無いと答え合わせが別の便の値段を拾う。'' は時刻が取れなかった便。
    departure_time: str = ""


def _row_to_watch(row) -> Watch:
    return Watch(
        id=row["id"],
        token=row["token"],
        origin=row["origin"],
        destination=row["destination"],
        departure_date=row["departure_date"],
        airline_filter=json.loads(row["airline_filter"]),
        max_total_minutes=row["max_total_minutes"],
        nearby_airports=bool(row["nearby_airports"]),
        notify_email=row["notify_email"],
        created_at=row["created_at"],
        purpose=row["purpose"] if "purpose" in row.keys() else "user",
        return_date=row["return_date"] if "return_date" in row.keys() else "",
        notified_low_usd=(row["notified_low_usd"]
                          if "notified_low_usd" in row.keys() else None),
        notified_rise_at=(row["notified_rise_at"]
                          if "notified_rise_at" in row.keys() else None),
        last_accessed_at=row["last_accessed_at"],
        expires_at=row["expires_at"],
    )


# ============================================================
# monitored_routes
# ============================================================

def upsert_monitored_route(origin: str, destination: str,
                            departure_date: str,
                            return_date: str = "") -> int:
    """収集対象を登録し、その id を返す。既にあれば再有効化して既存 id を返す。

    複数ユーザーが同じルート・同じ出発日を登録しても行は1本に集約される
    （UNIQUE 制約）。ここが「API コストがユーザー数ではなく異なるルート数に
    比例する」ことの実装上の根拠。
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO monitored_routes
                (origin, destination, departure_date, return_date,
                 active, first_seen_at)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT (origin, destination, departure_date, return_date)
            -- **利用者が改めて登録したら復活させる。** 自動停止の記録も消す。
            -- 状況が直っていなければ数日後にまた止まるだけで、無駄は数日分に
            -- 収まる。永久に死んだままにするより、直ったときに戻れるほうがよい。
            DO UPDATE SET active = 1, disabled_at = NULL, disabled_reason = NULL, disabled_kind = NULL
            """,
            (origin, destination, departure_date, return_date, _now()),
        )
        row = conn.execute(
            """
            SELECT id FROM monitored_routes
            WHERE origin = ? AND destination = ? AND departure_date = ?
              AND return_date = ?
            """,
            (origin, destination, departure_date, return_date),
        ).fetchone()
    return row["id"]


def research_only_route_ids(today: str) -> set[int]:
    """調査用のモニターだけが参照している稼働ルート。

    取得頻度を落としてよいのはこれだけ（config.research_cadence）。
    **利用者のモニターが1つでも参照していれば外す** — 利用者のグラフは毎日の
    点で描く。失効したモニターは数えない。
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.id FROM monitored_routes r
            WHERE r.active = 1
              AND EXISTS (SELECT 1 FROM watch_routes wr JOIN watches w ON w.id = wr.watch_id
                          WHERE wr.monitored_route_id = r.id AND w.purpose = 'research'
                            AND w.expires_at >= ?)
              AND NOT EXISTS (SELECT 1 FROM watch_routes wr JOIN watches w ON w.id = wr.watch_id
                              WHERE wr.monitored_route_id = r.id AND w.purpose = 'user'
                                AND w.expires_at >= ?)
            """,
            (today, today),
        ).fetchall()
    return {row["id"] for row in rows}


def active_routes() -> list[MonitoredRoute]:
    """日次収集ジョブが見る唯一のリスト。"""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, origin, destination, departure_date, return_date, active
            FROM monitored_routes
            WHERE active = 1
            ORDER BY departure_date, origin, destination
            """
        ).fetchall()
    return [
        MonitoredRoute(r["id"], r["origin"], r["destination"],
                       r["departure_date"], bool(r["active"]),
                       return_date=r["return_date"])
        for r in rows
    ]


def count_active_routes() -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM monitored_routes WHERE active = 1"
        ).fetchone()
    return row["c"]


def mark_route_collected(route_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE monitored_routes SET last_collected_at = ? WHERE id = ?",
            (_now(), route_id),
        )


def deactivate_orphan_routes() -> int:
    """どの watch からも参照されなくなったルートの収集を止める。

    行は消さない（active=0 にするだけ）。同じルートが再登録されたとき、
    first_seen_at と過去の紐付きを残しておきたいため。
    """
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE monitored_routes SET active = 0
            WHERE active = 1
              AND id NOT IN (SELECT monitored_route_id FROM watch_routes)
            """
        )
    return cur.rowcount


def save_fx_rate(pair: str, rate_date: str, rate: float) -> None:
    """同じ公表日を取り直したら上書きする（訂正が入ることがある）。"""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO fx_rates (pair, rate_date, rate, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (pair, rate_date) DO UPDATE
                SET rate = excluded.rate, fetched_at = excluded.fetched_at
            """,
            (pair, rate_date, rate, _now()),
        )


def latest_fx_rate(pair: str) -> Optional[dict]:
    """最新の1件。**取得できていなければ None**（表示側で USD に倒す）。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT rate_date, rate FROM fx_rates WHERE pair = ? "
            "ORDER BY rate_date DESC LIMIT 1",
            (pair,),
        ).fetchone()
    return {"date": row["rate_date"], "rate": row["rate"]} if row else None


def recent_collection_dates(limit: int) -> list[str]:
    """直近で収集が実際に走った日。新しい順。

    **カレンダーの日数ではなくこれを数える。** スケジューラが止まっていた日を
    「失敗した日」に数えると、こちらの都合でルートを止めることになる。
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT check_date FROM collection_logs "
            "ORDER BY check_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [r["check_date"] for r in rows]


def log_empty_search(check_date: str, origin: str, destination: str,
                     departure_date: str, stops: int,
                     return_date: str = "",
                     reason: Optional[str] = None) -> None:
    """検索は通ったが便が0件だったことを残す。

    **失敗ではないので fetch_errors には入れない。** 混ぜると KPI の誤り
    件数と collection_status が「運航されていない区間があるだけ」で悪化し、
    本当の不調が読めなくなる。

    `reason` は取得側が見た0件の理由（2026-10-12 から。それ以前の行は NULL）。
    error_status / empty_page / filtered / unknown。
    """
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO empty_searches "
            "(check_date, origin, destination, departure_date, return_date, "
            " stops, searched_at, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (check_date, origin, destination, departure_date, return_date,
             stops, _now(), reason),
        )


def routes_with_history(before_check_date: str) -> set[tuple[str, str, str, str]]:
    """指定日より前に一度でも実測が取れたルート。

    **0件の再検索の対象を決めるため。** 取れたことのあるルートが0件なら
    疑わしい（実在する路線が収集中にだけ空で返る、2026-10 の現象）。
    一度も取れていないルートの0件は正常なことが多い（予約受付前など）。
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT origin, destination, departure_date, return_date
            FROM snapshots
            WHERE source = 'real' AND check_date < ?
            """,
            (before_check_date,),
        ).fetchall()
    return {(r["origin"], r["destination"], r["departure_date"], r["return_date"])
            for r in rows}


def routes_failing_consistently(dates: Sequence[str]) -> list[dict]:
    """指定した収集日のすべてで実測が1件も取れなかったルート。

    **一部でも取れているルートは対象にしない。** 経由回数ごとに検索するので、
    stops=1 が落ちても stops=0 が取れていることがある（NRT-SYD がこれ）。
    そのルートは価値を出しているので止めてはいけない。

    **「試した証拠」を必ず求める。** 実測ゼロだけを条件にすると、昨日
    登録されたばかりで、まだ収集されていないルートを止めてしまう。
    証拠は2種類あり、両方を数える。

        エラーが出た           → fetch_errors    「取れなかった」
        検索は通って0件だった  → empty_searches  「便が無い」

    後者は 2026-08-30 に足した。それまでは前者しか見ていなかったため、
    **運航されていない区間（NRT-GMP など）が永久に止まらなかった** —
    例外が出ないので fetch_errors に何も残らず、収集ゼロのまま
    「モデル推定」を出し続けていた。

    ## 「便が無い」と判定するには、一度も取れたことが無いことを求める

    **2026-09-01 の歯止め。** 0件を停止の根拠に加えたことで、上流が
    不調な日を「路線が無い」と読む危険を作ってしまった。実際その日、
    FUK-HND・HND-OKA・LAX-NRT など**明らかに実在する路線**が17本まとめて
    0件を返した（上流の `IndexError` が同時多発した日）。あと1日続けば
    停止されていた。全面障害の歯止めは 17/447 = 3.8% で発動しない。

    **一度でも実価格が返ったことがあるなら、その区間に便はある。**
    その後0件が続くのは上流の問題であって、路線が無いのではない。
    `no_flights` は **全期間で実測ゼロ**のルートにしか付けない。
    取れていた路線が壊れた場合は従来どおり `fetch_failed` 側で扱う。

    `kind` で理由を言い分ける。利用者に見せる文言が変わる。

    出発日を過ぎたルートは対象外（別の仕組みで止まる）。
    """
    if not dates:
        return []
    marks = ",".join("?" * len(dates))
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT r.id, r.origin, r.destination, r.departure_date,
                   (SELECT COUNT(DISTINCT e.check_date) FROM fetch_errors e
                     WHERE e.origin = r.origin AND e.destination = r.destination
                       AND e.departure_date = r.departure_date
                       AND e.check_date IN ({marks}))            AS failed_days,
                   (SELECT COUNT(DISTINCT m.check_date) FROM empty_searches m
                     WHERE m.origin = r.origin AND m.destination = r.destination
                       AND m.departure_date = r.departure_date
                       AND m.check_date IN ({marks}))            AS empty_days,
                   (SELECT COUNT(*) FROM snapshots s
                     WHERE s.origin = r.origin AND s.destination = r.destination
                       AND s.departure_date = r.departure_date
                       AND s.source = 'real'
                       AND s.check_date IN ({marks}))            AS records,
                   -- **全期間**の実測。1件でもあれば「便が無い」ではない。
                   (SELECT COUNT(*) FROM snapshots s2
                     WHERE s2.origin = r.origin
                       AND s2.destination = r.destination
                       AND s2.departure_date = r.departure_date
                       AND s2.source = 'real')                   AS records_ever
            FROM monitored_routes r
            WHERE r.active = 1 AND r.departure_date >= ?
            """,
            (*dates, *dates, *dates, date.today().isoformat()),
        ).fetchall()

    out = []
    for r in rows:
        row = dict(r)
        if row["records"]:
            continue                    # 少しでも取れているなら止めない
        tried = max(row["failed_days"], row["empty_days"])
        if tried < len(dates):
            continue                    # 全日に試した証拠が無い
        # **どちらが主かで理由を分ける。** 全日0件が返っていて、しかも
        # **一度も取れたことが無い**ときだけ「便が無い」と言う。
        # 取れていた路線が0件になったのは上流の問題（2026-09-01）。
        row["kind"] = ("no_flights"
                       if row["empty_days"] >= len(dates)
                       and not row["records_ever"]
                       else "fetch_failed")
        out.append(row)
    return out


def disable_routes(route_ids: Sequence[int], reason: str,
                   kind: Optional[str] = None) -> int:
    """収集を止める。**行は消さない** — 理由と時刻を残して後から追える。"""
    if not route_ids:
        return 0
    marks = ",".join("?" * len(route_ids))
    with get_connection() as conn:
        cur = conn.execute(
            f"UPDATE monitored_routes SET active = 0, disabled_at = ?, "
            f"disabled_reason = ?, disabled_kind = ? WHERE id IN ({marks})",
            (_now(), reason, kind, *route_ids),
        )
    return cur.rowcount


def count_active_routes() -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM monitored_routes WHERE active = 1").fetchone()
    return row["c"]


def disabled_routes() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT origin, destination, departure_date, disabled_at, disabled_reason, "
            "disabled_kind "
            "FROM monitored_routes WHERE active = 0 AND disabled_reason IS NOT NULL "
            "ORDER BY disabled_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def deactivate_departed_routes(today: str) -> int:
    """出発日を過ぎたルートの収集を止める。"""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE monitored_routes SET active = 0 "
            "WHERE active = 1 AND departure_date < ?",
            (today,),
        )
    return cur.rowcount


# ============================================================
# watches
# ============================================================

def create_watch(origin: str, destination: str, departure_date: str,
                  route_ids: Sequence[int],
                  airline_filter: Optional[list[str]] = None,
                  max_total_minutes: Optional[int] = _UNSET,
                  nearby_airports: bool = False,
                  ip_hash: Optional[str] = None,
                  notify_email: Optional[str] = None,
                  purpose: str = "user",
                  return_date: str = "") -> Watch:
    """モニターを作り、収集対象ルートに紐付ける。

    route_ids が複数になるのは 2.3 の近接空港ルールがあるため
    （HND->SAN の watch は HND-SAN と HND-LAX の2ルートを必要とする）。
    したがって watch : monitored_route は多対一ではなく多対多。

    有効期限は「最終アクセスから idle 日数」と「出発日 + 猶予日数」の
    早い方。放置されたモニターの価格を無期限に取得し続けないため。
    """
    settings = get_settings()
    now = _now()
    token = new_token()

    # 既定で上限を入れる。入れないと、乗継が一晩ある便が「最安」として
    # 買い時判定に使われる。利用者は設定タブで解除できる。
    if max_total_minutes is _UNSET:
        max_total_minutes = settings.max_total_minutes_default

    expires_at = expires_at_for(departure_date)

    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO watches
                (token, origin, destination, departure_date, return_date,
                 airline_filter,
                 max_total_minutes, nearby_airports, notify_email,
                 purpose, created_from_ip_hash, created_at,
                 last_accessed_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (token, origin, destination, departure_date, return_date,
             json.dumps(airline_filter or []), max_total_minutes,
             int(nearby_airports), notify_email, purpose,
             ip_hash, now, now, expires_at),
        )
        watch_id = cur.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO watch_routes (watch_id, monitored_route_id) "
            "VALUES (?, ?)",
            [(watch_id, rid) for rid in route_ids],
        )

    return Watch(
        id=watch_id, token=token, origin=origin, destination=destination,
        departure_date=departure_date, airline_filter=airline_filter or [],
        max_total_minutes=max_total_minutes,
        nearby_airports=nearby_airports, purpose=purpose,
        return_date=return_date,
        notify_email=notify_email, created_at=now,
        last_accessed_at=now, expires_at=expires_at,
    )


# ============================================================
# 一覧（collections）
# ============================================================

@dataclass(frozen=True)
class Collection:
    id: int
    token: str
    created_at: str
    last_accessed_at: str
    expires_at: str


def _row_to_collection(row) -> Collection:
    return Collection(id=row["id"], token=row["token"],
                      created_at=row["created_at"],
                      last_accessed_at=row["last_accessed_at"],
                      expires_at=row["expires_at"])


def _collection_expiry(conn, collection_id: int, now: datetime) -> str:
    """一覧の有効期限。**中身より先に消さない。**

    先に消えると「一覧を失う」という、この仕組みを作った理由そのものが
    起きる。所属モニターの最長の有効期限に合わせ、空なら放置期限で切る。
    """
    settings = get_settings()
    row = conn.execute(
        """
        SELECT MAX(w.expires_at) AS latest FROM collection_watches cw
        JOIN watches w ON w.id = cw.watch_id
        WHERE cw.collection_id = ?
        """,
        (collection_id,),
    ).fetchone()
    idle = (now + timedelta(days=settings.collection_idle_expiry_days)).date()
    latest = row["latest"] if row else None
    if not latest:
        return idle.isoformat()
    contents = date.fromisoformat(latest) + timedelta(
        days=settings.watch_post_departure_grace_days)
    return max(contents, idle).isoformat()


def create_collection(ip_hash: Optional[str] = None) -> Collection:
    now = _now()
    token = new_token()
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO collections
                (token, created_from_ip_hash, created_at, last_accessed_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (token, ip_hash, now, now,
             (datetime.fromisoformat(now)
              + timedelta(days=get_settings().collection_idle_expiry_days)).date().isoformat()),
        )
        return Collection(id=cur.lastrowid, token=token, created_at=now,
                          last_accessed_at=now,
                          expires_at=_collection_expiry(conn, cur.lastrowid,
                                                        datetime.fromisoformat(now)))


def get_collection_by_token(token: str) -> Optional[Collection]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM collections WHERE token = ? AND expires_at >= ?",
            (token, date.today().isoformat()),
        ).fetchone()
    return _row_to_collection(row) if row else None


def touch_collection(token: str) -> None:
    """アクセスされている限り生かす。期限も中身に合わせて引き直す。"""
    now = _now()
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM collections WHERE token = ?",
                           (token,)).fetchone()
        if not row:
            return
        conn.execute(
            "UPDATE collections SET last_accessed_at = ?, expires_at = ? WHERE id = ?",
            (now, _collection_expiry(conn, row["id"], datetime.fromisoformat(now)),
             row["id"]),
        )


def watches_in_collection(token: str) -> list[Watch]:
    """一覧に入っているモニター。**サーバ側が正。**"""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT w.* FROM collection_watches cw
            JOIN collections c ON c.id = cw.collection_id
            JOIN watches w     ON w.id = cw.watch_id
            WHERE c.token = ?
            ORDER BY cw.added_at DESC, w.id DESC
            """,
            (token,),
        ).fetchall()
    return [_row_to_watch(r) for r in rows]


def add_watch_to_collection(collection_token: str, watch_token: str) -> bool:
    """一覧にモニターを入れる。既に入っていれば何もしない（冪等）。"""
    now = _now()
    with get_connection() as conn:
        c = conn.execute("SELECT id FROM collections WHERE token = ?",
                         (collection_token,)).fetchone()
        w = conn.execute("SELECT id FROM watches WHERE token = ?",
                         (watch_token,)).fetchone()
        if not c or not w:
            return False
        conn.execute(
            "INSERT OR IGNORE INTO collection_watches "
            "(collection_id, watch_id, added_at) VALUES (?, ?, ?)",
            (c["id"], w["id"], now),
        )
        conn.execute(
            "UPDATE collections SET expires_at = ? WHERE id = ?",
            (_collection_expiry(conn, c["id"], datetime.fromisoformat(now)), c["id"]),
        )
    return True


def remove_watch_from_collection(collection_token: str, watch_token: str) -> bool:
    """一覧から外す。**モニター自体は消さない。**"""
    with get_connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM collection_watches
            WHERE collection_id = (SELECT id FROM collections WHERE token = ?)
              AND watch_id      = (SELECT id FROM watches     WHERE token = ?)
            """,
            (collection_token, watch_token),
        )
    return cur.rowcount > 0


def delete_expired_collections(today: Optional[str] = None) -> int:
    today = today or date.today().isoformat()
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM collections WHERE expires_at < ?", (today,))
    return cur.rowcount


def link_watch_routes(watch_id: int, route_ids: Sequence[int]) -> int:
    """モニターと収集対象ルートを紐付ける。既に紐付いていれば何もしない。

    近接空港ルールを広げたときなど、**登録済みモニターに後からルートを
    足す**ために切り出してある（expand_routes は登録時にしか走らないので、
    ルールを変えても既存モニターには反映されない）。
    """
    with get_connection() as conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO watch_routes (watch_id, monitored_route_id) "
            "VALUES (?, ?)",
            [(watch_id, rid) for rid in route_ids],
        )
    return cur.rowcount


def refresh_expiry(token: str) -> Optional[str]:
    """いまの規則で失効日を計算し直す。**last_accessed_at は触らない。**

    設定を変えても既存の watches 行には反映されないので、後から流し直す
    ための入口。touch_watch を使うと「利用者がアクセスした」ことになって
    しまうので分けてある。
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT departure_date FROM watches WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return None
        expires = expires_at_for(row["departure_date"])
        conn.execute("UPDATE watches SET expires_at = ? WHERE token = ?",
                     (expires, token))
    return expires


def all_watches() -> list[Watch]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM watches ORDER BY id").fetchall()
    return [_row_to_watch(r) for r in rows]


def get_watch_by_token(token: str) -> Optional[Watch]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM watches WHERE token = ?", (token,)
        ).fetchone()
    return _row_to_watch(row) if row else None


def touch_watch(token: str) -> None:
    """最終アクセス日時を更新し、失効を先送りする。

    localStorage を消してもブックマークした URL から復旧できる必要があるため、
    「アクセスされている限り生きている」を last_accessed_at で表現する。
    """
    settings = get_settings()
    now_dt = datetime.now()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT departure_date FROM watches WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return
        conn.execute(
            "UPDATE watches SET last_accessed_at = ?, expires_at = ? WHERE token = ?",
            (now_dt.isoformat(timespec="seconds"),
             expires_at_for(row["departure_date"], now_dt), token),
        )


def set_nearby_airports(token: str, enabled: bool) -> bool:
    """近接空港ルールの on/off を切り替える。

    ルートの張り替えはここではしない（呼び出し側が sync_watch_routes を
    呼ぶ）。SQL と業務ロジックを混ぜないため。
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE watches SET nearby_airports = ? WHERE token = ?",
            (int(enabled), token),
        )
    return cur.rowcount > 0


def unlink_watch_routes(watch_id: int, keep_route_ids: Sequence[int]) -> int:
    """指定した以外のルートとの紐付けを外す。

    近接空港ルールを切ったときに、不要になったルートを外すために使う。
    **monitored_routes の行自体も snapshots も消さない** — 再度 on に
    したときに過去の価格履歴をそのまま見せられるようにする。
    """
    placeholders = ",".join("?" for _ in keep_route_ids) or "NULL"
    with get_connection() as conn:
        cur = conn.execute(
            f"DELETE FROM watch_routes WHERE watch_id = ? "
            f"AND monitored_route_id NOT IN ({placeholders})",
            (watch_id, *keep_route_ids),
        )
    return cur.rowcount


def update_max_total_minutes(token: str, minutes: Optional[int]) -> bool:
    """総所要時間の上限を差し替える。None = 上限なし。"""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE watches SET max_total_minutes = ? WHERE token = ?",
            (minutes, token),
        )
    return cur.rowcount > 0


def update_airline_filter(token: str, airlines: Sequence[str]) -> bool:
    """モニター対象の航空会社を差し替える。

    空リスト = しぼり込みなし（全社）。トークンを知っていれば誰でも変えられる
    のは仕様（capability として扱う）。
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE watches SET airline_filter = ? WHERE token = ?",
            (json.dumps(list(airlines)), token),
        )
    return cur.rowcount > 0


def delete_watch(token: str) -> bool:
    """モニターを削除する。watch_routes は ON DELETE CASCADE で消える。

    価格履歴（snapshots）は消さない。収集済みの価格は資産として残し、
    同じルートを誰かが再登録したとき即座に過去トレンドを見せられるようにする。
    """
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM watches WHERE token = ?", (token,))
    return cur.rowcount > 0


def delete_expired_watches(today: str) -> int:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM watches WHERE expires_at < ?", (today,))
    return cur.rowcount


def count_watches_created_since(ip_hash: str, since_iso: str) -> int:
    """IP あたりの登録レート制限用。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM watches "
            "WHERE created_from_ip_hash = ? AND created_at >= ?",
            (ip_hash, since_iso),
        ).fetchone()
    return row["c"]


def count_active_watches_for_ip(ip_hash: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM watches WHERE created_from_ip_hash = ?",
            (ip_hash,),
        ).fetchone()
    return row["c"]


def routes_for_watch(token: str) -> list[MonitoredRoute]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.origin, r.destination, r.departure_date,
                   r.return_date, r.active, r.disabled_reason,
                   r.disabled_kind
            FROM monitored_routes r
            JOIN watch_routes wr ON wr.monitored_route_id = r.id
            JOIN watches w       ON w.id = wr.watch_id
            WHERE w.token = ?
            ORDER BY r.origin, r.destination
            """,
            (token,),
        ).fetchall()
    return [
        MonitoredRoute(r["id"], r["origin"], r["destination"],
                       r["departure_date"], bool(r["active"]),
                       r["disabled_reason"], r["disabled_kind"],
                       return_date=r["return_date"])
        for r in rows
    ]


# ============================================================
# snapshots
# ============================================================

def upsert_snapshots(snapshots: Iterable[Snapshot]) -> int:
    """価格スナップショットを upsert する。

    via_airport は None を '' に落として保存する。SQLite の UNIQUE 制約は
    NULL != NULL と評価するため、NULL のままだと nonstop 行の重複排除が
    黙って壊れる（既存 flight-price-monitor で判明済みの罠）。
    """
    rows = [
        (s.check_date, s.departure_date, s.return_date, s.origin,
         s.destination, s.stops,
         s.via_airport or "", s.airline, s.price_usd, s.source, s.fetched_at,
         s.departure_time, s.arrival_time, s.air_minutes, s.total_minutes,
         s.plane_types, s.carbon_g)
        for s in snapshots
    ]
    if not rows:
        return 0
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO snapshots
                (check_date, departure_date, return_date, origin, destination,
                 stops, via_airport, airline, price_usd, source, fetched_at,
                 departure_time, arrival_time, air_minutes, total_minutes,
                 plane_types, carbon_g)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            -- **departure_time まで含める。** 同じ社・同じ経由地で1日に
            -- 何本も飛ぶので、ここを外すと2本が1行を奪い合い、安いほうだけが
            -- 残って日ごとに別の便が同じ線につながる。
            ON CONFLICT (check_date, departure_date, return_date, origin,
                         destination, stops, via_airport, airline,
                         departure_time)
            DO UPDATE SET price_usd      = excluded.price_usd,
                          source         = excluded.source,
                          fetched_at     = excluded.fetched_at,
                          -- departure_time は鍵なので書き換えない
                          arrival_time   = excluded.arrival_time,
                          air_minutes    = excluded.air_minutes,
                          total_minutes  = excluded.total_minutes,
                          plane_types    = excluded.plane_types,
                          carbon_g       = excluded.carbon_g
            """,
            rows,
        )
    return len(rows)


def history_for(origin: str, destination: str, departure_date: str,
                 sources: Sequence[str] = ("real",),
                 return_date: str = "") -> list[dict]:
    """1ルート・1出発日の価格履歴。

    sources のデフォルトが ('real',) であることが重要。既存
    flight-price-monitor の predict.py は source を区別せず全行を返していたため、
    バックフィルしたシミュレーション値が「実測」として較正に混入し、
    最安値として推奨されうる状態になっていた。既定を実測のみにして、
    シミュレーション値が要る場面（グラフの補助線など）でのみ明示的に広げる。
    """
    placeholders = ",".join("?" for _ in sources)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM snapshots
            WHERE origin = ? AND destination = ? AND departure_date = ?
              -- **必ず指定する。** 省くと往復の行（金額がほぼ2倍）が
              -- 片道の系列に混ざり、較正が静かに狂う。
              AND return_date = ?
              AND source IN ({placeholders})
            ORDER BY check_date, price_usd
            """,
            (origin, destination, departure_date, return_date, *sources),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["via_airport"] = d["via_airport"] or None
        out.append(d)
    return out


def count_long_itineraries(origin: str, destination: str, departure_date: str,
                            max_total_minutes: Optional[int],
                            return_date: str = "") -> int:
    """所要時間の上限で外れる便の数。

    **最新の観測日だけを見る。** 全期間で数えると、過去に一度出ただけの便が
    ずっと件数に乗り、「いま何件隠れているか」を表さなくなる。
    総所要が不明な行は数えない（隠していないため）。
    """
    if not max_total_minutes or max_total_minutes <= 0:
        return 0
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM (
                -- **便ごとに数える。** 同じ社・同じ経由地で1日に何本も
                -- 飛ぶので、出発時刻まで見ないと隠れた便を数え落とす。
                SELECT DISTINCT stops, via_airport, airline, departure_time
                FROM snapshots
                WHERE origin = ? AND destination = ? AND departure_date = ?
                  AND return_date = ?
                  AND source = 'real'
                  AND total_minutes IS NOT NULL AND total_minutes > ?
                  AND check_date = (
                      SELECT MAX(check_date) FROM snapshots
                      WHERE origin = ? AND destination = ?
                        AND departure_date = ? AND return_date = ?
                        AND source = 'real')
            )
            """,
            (origin, destination, departure_date, return_date, max_total_minutes,
             origin, destination, departure_date, return_date),
        ).fetchone()
    return row["c"]


def route_history(origin: str, destination: str,
                   since_check_date: Optional[str] = None,
                   exclude_departure_date: Optional[str] = None,
                   return_date: str = "") -> list[dict]:
    """**出発日を問わず**、このルートで実際に観測された運賃。

    「まだ収集していない出発日」の推定に使う。同じ (出発地, 到着地, 便) なら、
    出発日が違っても曲線からのズレ（スケール）はおおむね引き継げる — 出発日
    そのものの効果（季節・曜日・出発までの日数）は曲線側が既に織り込んで
    いるため。

    `source='real'` に限るのは history_for と同じ理由。ここを緩めると
    シミュレーション値が「観測された航空会社」として扱われてしまう。
    """
    clauses = ["origin = ?", "destination = ?", "return_date = ?",
               "source = 'real'"]
    params: list = [origin, destination, return_date]
    if since_check_date:
        # 何年も前に撤退した航空会社を「就航中」として扱わない。
        clauses.append("check_date >= ?")
        params.append(since_check_date)
    if exclude_departure_date:
        clauses.append("departure_date != ?")
        params.append(exclude_departure_date)

    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM snapshots WHERE {' AND '.join(clauses)} "
            "ORDER BY check_date",
            params,
        ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["via_airport"] = d["via_airport"] or None
        out.append(d)
    return out


def airlines_on_route(origin: str, destination: str,
                       since_check_date: Optional[str] = None,
                       return_date: str = "") -> list[str]:
    """このルートで実際に観測された航空会社名。

    しぼり込みの選択肢に使う。曲線の係数表から候補を出すと、その社が飛んで
    いない路線まで選べてしまうので、実測に出た社だけを並べる。
    """
    clauses = ["origin = ?", "destination = ?", "return_date = ?",
               "source = 'real'"]
    params: list = [origin, destination, return_date]
    if since_check_date:
        clauses.append("check_date >= ?")
        params.append(since_check_date)

    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT airline FROM snapshots "
            f"WHERE {' AND '.join(clauses)} ORDER BY airline",
            params,
        ).fetchall()
    return [r["airline"] for r in rows]


# ============================================================
# エクスポート（学習・分析用）
# ============================================================

def export_snapshots(sources: Sequence[str] = ("real",),
                     completed_only: bool = False,
                     today: Optional[str] = None) -> list[dict]:
    """観測した価格を全部返す。

    `completed_only` は「出発日を迎えた系列だけ」に絞る。学習に使えるのは
    出発日まで走りきった系列だけ — 途中で切れた列は「残り期間の最小値が
    いくらだったか」という正解が確定しないため。

    行数は年間でも数十万のオーダーなので、まとめて返して問題ない。
    """
    clauses = [f"source IN ({','.join('?' for _ in sources)})"]
    params: list = list(sources)
    if completed_only:
        clauses.append("departure_date < ?")
        params.append(today or date.today().isoformat())

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT check_date, departure_date, return_date,
                   origin, destination, stops,
                   via_airport, airline, price_usd, source, fetched_at,
                   departure_time, arrival_time, air_minutes, plane_types, carbon_g,
                   CAST(julianday(departure_date) - julianday(check_date) AS INTEGER)
                       AS days_to_departure
            FROM snapshots
            WHERE {' AND '.join(clauses)}
            ORDER BY origin, destination, departure_date, airline, check_date
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def export_predictions() -> list[dict]:
    """予測とその後の実測を突き合わせた行を返す（精度検証用）。

    prediction_log は「◯日前にいくらと予測したか」、snapshots は「その日に
    実際にいくらだったか」を持つ。**同じ便で突き合わせるのはここだけ**なので、
    JOIN をリポジトリ層に置いてある。
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.predicted_on, p.target_check_date, p.departure_date,
                   p.origin, p.destination, p.stops, p.via_airport, p.airline,
                   p.predicted_usd, p.lower_usd, p.upper_usd, p.observed_points,
                   CAST(julianday(p.target_check_date)
                        - julianday(p.predicted_on) AS INTEGER) AS horizon_days,
                   s.price_usd AS actual_usd
            FROM prediction_log p
            LEFT JOIN snapshots s
                   ON s.check_date     = p.target_check_date
                  AND s.departure_date = p.departure_date
                  AND s.origin         = p.origin
                  AND s.destination    = p.destination
                  AND s.stops          = p.stops
                  AND s.via_airport    = p.via_airport
                  AND s.airline        = p.airline
                  AND s.source         = 'real'
            ORDER BY p.predicted_on, p.target_check_date
            """
        ).fetchall()
    return [dict(r) for r in rows]


def dataset_summary(today: Optional[str] = None) -> dict:
    """学習データとして見たときの現状。"""
    today = today or date.today().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM snapshots WHERE source='real')          AS real_rows,
              (SELECT COUNT(DISTINCT check_date) FROM snapshots
                WHERE source='real')                                        AS observed_days,
              (SELECT COUNT(*) FROM (
                 SELECT DISTINCT origin, destination, departure_date, return_date
                 FROM snapshots WHERE source='real'))                       AS series,
              (SELECT COUNT(*) FROM (
                 SELECT DISTINCT origin, destination, departure_date, return_date
                 FROM snapshots WHERE source='real' AND departure_date < ?)) AS completed_series,
              (SELECT MIN(check_date) FROM snapshots WHERE source='real')   AS first_check,
              (SELECT MAX(check_date) FROM snapshots WHERE source='real')   AS last_check,
              (SELECT COUNT(*) FROM prediction_log)                         AS predictions
            """,
            (today,),
        ).fetchone()
    return dict(row)


def latest_check_date() -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(check_date) AS d FROM snapshots").fetchone()
    return row["d"] if row and row["d"] else None


# ============================================================
# prediction_log
# ============================================================

def insert_predictions(predictions: Iterable[Prediction]) -> int:
    rows = [
        (p.predicted_on, p.target_check_date, p.departure_date,
         p.return_date, p.origin,
         p.destination, p.stops, p.via_airport or "", p.airline,
         p.departure_time or "",
         p.predicted_usd, p.lower_usd, p.upper_usd, p.observed_points, _now())
        for p in predictions
    ]
    if not rows:
        return 0
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO prediction_log
                (predicted_on, target_check_date, departure_date, return_date,
                 origin, destination, stops, via_airport, airline,
                 departure_time,
                 predicted_usd, lower_usd, upper_usd, observed_points, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (predicted_on, target_check_date, departure_date,
                         return_date, origin, destination, stops, via_airport,
                         airline, departure_time)
            DO UPDATE SET predicted_usd   = excluded.predicted_usd,
                          lower_usd       = excluded.lower_usd,
                          upper_usd       = excluded.upper_usd,
                          observed_points = excluded.observed_points
            """,
            rows,
        )
    return len(rows)


# ============================================================
# ログ
# ============================================================

def insert_collection_log(check_date: str, status: str, source: str,
                           routes_checked: int, records_saved: int,
                           errors: Optional[list[str]], duration_ms: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO collection_logs
                (check_date, status, source, routes_checked, records_saved,
                 errors, duration_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (check_date, status, source, routes_checked, records_saved,
             json.dumps(errors) if errors else None, duration_ms, _now()),
        )


def latest_collection_log() -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM collection_logs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def fetch_error_summary(since_check_date: str) -> list[dict]:
    """取得失敗を (出発地, 到着地, 経由回数) ごとにまとめる。

    **失敗そのものは抑制しない。** 恒常的に壊れている組み合わせがあると
    `status` は毎日 partial になり、警告として機能しなくなる。そこで
    「いつから出ているか」を返し、**今日はじめて出たものだけを新規として
    浮かび上がらせる**（app/cli.py の health）。

    既知リストで握りつぶす方式は採らない。Google 側の変更でパーサが全面的に
    壊れたとき、リストに入っている分は永久に気づけなくなるため。
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT origin, destination, stops,
                   MIN(check_date)            AS first_seen,
                   MAX(check_date)            AS last_seen,
                   COUNT(DISTINCT check_date) AS days_seen,
                   COUNT(*)                   AS occurrences,
                   MAX(error_message)         AS sample
            FROM fetch_errors
            WHERE check_date >= ?
            GROUP BY origin, destination, stops
            ORDER BY last_seen DESC, days_seen DESC
            """,
            (since_check_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def collection_trend(since_check_date: str) -> list[dict]:
    """日ごとの収集結果。**その日の最後の実行だけ**を採る。

    手で流し直した日は collection_logs に複数行入る。最新の実行が
    その日の最終状態なので、それだけを見る。
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.check_date, c.status, c.routes_checked, c.records_saved,
                   c.duration_ms,
                   (SELECT COUNT(*) FROM fetch_errors e
                     WHERE e.check_date = c.check_date) AS errors
            FROM collection_logs c
            JOIN (SELECT check_date, MAX(id) AS id
                    FROM collection_logs GROUP BY check_date) latest
              ON latest.id = c.id
            WHERE c.check_date >= ?
            ORDER BY c.check_date
            """,
            (since_check_date,),
        ).fetchall()
    return [dict(r) for r in rows]


# 作成直後の初回表示を「再訪」と数えないための猶予。
#
# **created_at と last_accessed_at は作成時に同じ値が入る。** 直後に
# ブラウザが /w/{token} を開いて GET を投げるので、last_accessed_at は
# 数秒だけ進む。これを再訪と数えると、全件が「見られている」ことになる。
#
# 30分にしたのは、作った直後に眺めて回る一連の操作までを1回の来訪と
# 見なすため。それを越えて戻ってきたなら、本当に戻ってきたと数える。
REVISIT_GRACE_MINUTES = 30


def kpi_snapshot(today: str, since_24h: str, since_7d: str) -> dict:
    """その日の KPI 一式。

    利用状況（需要）・容量・蓄積したデータ・健全性の4面を1回で取る。
    将来の拡張判断に使うので、**上限に対する余裕**が読めることを重視した。

    `watches_created_last_24h` を「今日作られた数」にしていないのは、
    created_at がサーバ時刻（UTC）なのに対し check_date は TIMEZONE 基準で、
    日付の境目がずれるため。24時間窓なら基準が揃う。

    **再訪の3列は「作られただけか、使われているか」を分けるために置いた。**
    それまでの列は登録の勢いしか映しておらず、110件のモニターが一度も
    開き直されていなくても KPI は好調に見えていた。
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM watches WHERE purpose = 'user')           AS watches_total,
              (SELECT COUNT(*) FROM watches WHERE purpose = 'user'
                AND expires_at >= ?)                                          AS watches_active,
              (SELECT COUNT(*) FROM watches WHERE purpose = 'user'
                AND created_at >= ?)                                          AS watches_created_last_24h,
              (SELECT COUNT(DISTINCT created_from_ip_hash) FROM watches
                WHERE created_from_ip_hash IS NOT NULL
                  AND purpose = 'user')                                       AS distinct_creators,
              (SELECT COUNT(*) FROM watches WHERE purpose = 'user'
                AND last_accessed_at >= ?)                                    AS watches_accessed_last_24h,
              (SELECT COUNT(*) FROM watches WHERE purpose = 'user'
                AND last_accessed_at >= ?)                                    AS watches_accessed_last_7d,
              -- 作成直後の初回表示しか無いもの。「作られただけ」の件数。
              --
              -- **両辺を datetime() に通すこと。** 保存値は 'T' 区切りの ISO
              -- だが datetime() は空白区切りを返す。素の文字列比較だと
              -- 'T'(0x54) > ' '(0x20) で常に偽になり、この列が永久に 0 になる。
              (SELECT COUNT(*) FROM watches
                WHERE purpose = 'user'
                  AND datetime(last_accessed_at) <
                      datetime(created_at, '+' || ? || ' minutes'))           AS watches_never_reopened,
              -- **隠さずに数を出す。** 需要の列から外したぶん、何件外したかが
              -- 分からないと CSV だけ見た人が総数を誤解する。
              (SELECT COUNT(*) FROM watches WHERE purpose = 'research')       AS watches_research,
              (SELECT COUNT(*) FROM monitored_routes WHERE active = 1)        AS routes_active,
              (SELECT COUNT(*) FROM snapshots WHERE source = 'real')          AS snapshots_real,
              (SELECT COUNT(*) FROM snapshots WHERE source = 'simulated')     AS snapshots_simulated,
              (SELECT COUNT(DISTINCT check_date) FROM snapshots
                WHERE source = 'real')                                        AS observed_days,
              (SELECT COUNT(*) FROM (SELECT DISTINCT origin, destination, departure_date, return_date
                 FROM snapshots WHERE source = 'real'))                       AS series_total,
              (SELECT COUNT(*) FROM (SELECT DISTINCT origin, destination, departure_date, return_date
                 FROM snapshots WHERE source = 'real' AND departure_date < ?)) AS series_completed,
              (SELECT COUNT(*) FROM (SELECT DISTINCT origin, destination
                 FROM snapshots WHERE source = 'real'))                       AS distinct_markets,
              (SELECT COUNT(DISTINCT departure_date) FROM snapshots
                WHERE source = 'real')                                        AS distinct_departure_dates,
              (SELECT COUNT(DISTINCT airline) FROM snapshots
                WHERE source = 'real' AND airline != '')                      AS distinct_airlines,
              (SELECT COUNT(*) FROM prediction_log)                           AS predictions_logged,
              (SELECT COUNT(*) FROM fetch_errors WHERE check_date = ?)        AS fetch_errors,
              (SELECT status FROM collection_logs WHERE check_date = ?
                ORDER BY id DESC LIMIT 1)                                     AS collection_status,
              (SELECT COALESCE(SUM(routes_checked), 0) FROM collection_logs
                WHERE check_date = ?)                                         AS routes_checked,
              (SELECT COALESCE(SUM(records_saved), 0) FROM collection_logs
                WHERE check_date = ?)                                         AS records_collected,
              (SELECT COALESCE(SUM(duration_ms), 0) FROM collection_logs
                WHERE check_date = ?)                                         AS collection_duration_ms
            """,
            (today, since_24h, since_24h, since_7d, REVISIT_GRACE_MINUTES,
             today, today, today, today, today, today),
        ).fetchone()
    return dict(row)


def log_fetch_error(check_date: str, origin: str, destination: str,
                     departure_date: str, stops: int,
                     via_airport: Optional[str], error_message: str,
                     return_date: str = "") -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO fetch_errors
                (check_date, origin, destination, departure_date, return_date,
                 stops, via_airport, error_message, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (check_date, origin, destination, departure_date, return_date,
             stops, via_airport, error_message, _now()),
        )


# ----------------------------------------------------------------------
# price_history — 取得元が持っていた過去の価格
# ----------------------------------------------------------------------

def save_price_history(origin: str, destination: str, departure_date: str,
                       points: Sequence[tuple[str, float]],
                       return_date: str = "") -> int:
    """まとめて保存し、**書き込んだ行数**を返す。

    同じ観測日を取り直したら上書きする。取得元が過去分を訂正することがあり、
    そのときは新しい写しを正とする。

    件数は `rowcount` ではなく渡された数から数える。SQLite の executemany は
    ON CONFLICT DO UPDATE を含むと rowcount が当てにならない。
    """
    if not points:
        return 0
    now = _now()
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO price_history
                (origin, destination, departure_date, return_date,
                 observed_date, price_usd, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (origin, destination, departure_date, return_date,
                         observed_date)
                DO UPDATE SET price_usd  = excluded.price_usd,
                              captured_at = excluded.captured_at
            """,
            [(origin, destination, departure_date, return_date, d, p, now)
             for d, p in points],
        )
    return len(points)


def price_history(origin: str, destination: str,
                  departure_date: str, return_date: str = "") -> list[dict]:
    """1ルート分の履歴を古い順に。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT observed_date, price_usd FROM price_history "
            "WHERE origin = ? AND destination = ? AND departure_date = ? "
            "AND return_date = ? ORDER BY observed_date",
            (origin, destination, departure_date, return_date),
        ).fetchall()
    return [{"observed_date": r["observed_date"], "price_usd": r["price_usd"]}
            for r in rows]


def routes_without_history() -> list[dict]:
    """稼働中で、まだ履歴を取り込んでいないルート。

    **遡り取り込みは1ルート1回でよい。** 翌日以降の1点は snapshots として
    自分で観測しているので、取り直しても同じものが増えるだけ。
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.origin, r.destination, r.departure_date, r.return_date
            FROM monitored_routes r
            WHERE r.active = 1
              AND r.departure_date >= date('now')
              AND NOT EXISTS (
                  SELECT 1 FROM price_history h
                  WHERE h.origin = r.origin
                    AND h.destination = r.destination
                    AND h.departure_date = r.departure_date
                    AND h.return_date = r.return_date)
            ORDER BY r.departure_date
            """
        ).fetchall()
    return [dict(r) for r in rows]


def price_history_summary() -> dict:
    """取り込み状況の一覧。CLI とテストの確認用。"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS points, "
            "       COUNT(DISTINCT origin || destination || departure_date) AS routes, "
            "       MIN(observed_date) AS since, MAX(observed_date) AS until "
            "FROM price_history"
        ).fetchone()
    return dict(row)


# ============================================================
# push_subscriptions — Web Push の購読（課題4）
# ============================================================

@dataclass(frozen=True)
class PushSubscription:
    id: int
    endpoint: str
    p256dh: str
    auth: str
    lang: str = "en"
    last_sent_at: Optional[str] = None


def _row_to_subscription(row) -> PushSubscription:
    return PushSubscription(
        id=row["id"], endpoint=row["endpoint"], p256dh=row["p256dh"],
        auth=row["auth"], lang=row["lang"], last_sent_at=row["last_sent_at"],
    )


def upsert_push_subscription(endpoint: str, p256dh: str, auth: str,
                             lang: str = "en") -> int:
    """購読を登録し、その id を返す。同じ endpoint なら鍵を更新する。

    **同じ端末が再購読すると endpoint は同じで鍵だけ変わることがある**
    （ブラウザが購読を作り直したとき）。古い鍵のまま送ると復号できず、
    利用者には「許可したのに来ない」としか見えない。上書きする。
    """
    now = _now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions
                (endpoint, p256dh, auth, lang, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (endpoint) DO UPDATE SET
                p256dh       = excluded.p256dh,
                auth         = excluded.auth,
                lang         = excluded.lang,
                last_seen_at = excluded.last_seen_at
            """,
            (endpoint, p256dh, auth, lang, now, now),
        )
        row = conn.execute(
            "SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        ).fetchone()
    return row["id"]


def link_push_subscription(subscription_id: int, watch_token: str) -> bool:
    """購読を1件のモニターに紐付ける。モニターが無ければ False。"""
    with get_connection() as conn:
        watch = conn.execute(
            "SELECT id FROM watches WHERE token = ?", (watch_token,)
        ).fetchone()
        if watch is None:
            return False
        conn.execute(
            "INSERT OR IGNORE INTO push_subscription_watches "
            "(subscription_id, watch_id, created_at) VALUES (?, ?, ?)",
            (subscription_id, watch["id"], _now()),
        )
    return True


def unlink_push_subscription(endpoint: str, watch_token: str) -> bool:
    """1件のモニターの通知だけをやめる。購読そのものは残す。"""
    with get_connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM push_subscription_watches
            WHERE subscription_id = (SELECT id FROM push_subscriptions
                                     WHERE endpoint = ?)
              AND watch_id = (SELECT id FROM watches WHERE token = ?)
            """,
            (endpoint, watch_token),
        )
    return cur.rowcount > 0


def delete_push_subscription(endpoint: str) -> bool:
    """購読を消す。**送信時に 404/410 が返ったらこれを呼ぶ。**

    利用者がブラウザ側で切ったことは、こちらからは分からない。送って
    はじめて分かるので、死活監視はせず送信の失敗で掃除する。
    """
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    return cur.rowcount > 0


def push_subscriptions_for_watch(watch_id: int) -> list[PushSubscription]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.endpoint, s.p256dh, s.auth, s.lang, s.last_sent_at
            FROM push_subscriptions s
            JOIN push_subscription_watches w ON w.subscription_id = s.id
            WHERE w.watch_id = ?
            ORDER BY s.id
            """,
            (watch_id,),
        ).fetchall()
    return [_row_to_subscription(r) for r in rows]


def watch_tokens_for_subscription(endpoint: str) -> list[str]:
    """その端末が、どのモニターの通知を受け取る設定になっているか。"""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT w.token
            FROM watches w
            JOIN push_subscription_watches psw ON psw.watch_id = w.id
            JOIN push_subscriptions s ON s.id = psw.subscription_id
            WHERE s.endpoint = ?
            ORDER BY w.id
            """,
            (endpoint,),
        ).fetchall()
    return [r["token"] for r in rows]


def touch_push_subscription(endpoint: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE push_subscriptions SET last_seen_at = ? WHERE endpoint = ?",
            (_now(), endpoint))


def count_push_subscriptions() -> int:
    with get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM push_subscriptions").fetchone()["c"]


def watches_with_push_subscribers() -> list[Watch]:
    """購読者がいるモニターだけ。**通知の判定はこれだけを見る。**

    全モニターを回すと、誰も待っていない450件ぶんの予測計算を毎日走らせる
    ことになる。購読が付いているものだけで足りる。
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT w.* FROM watches w
            JOIN push_subscription_watches psw ON psw.watch_id = w.id
            ORDER BY w.id
            """
        ).fetchall()
    return [_row_to_watch(r) for r in rows]


def mark_notified_low(watch_id: int, price_usd: float) -> None:
    """①で知らせた最安を記録する。**二度同じことを送らないため。**

    日次ジョブが再実行されても、この値と比べるので再送しない。
    """
    with get_connection() as conn:
        conn.execute("UPDATE watches SET notified_low_usd = ? WHERE id = ?",
                     (price_usd, watch_id))


def mark_notified_rise(watch_id: int, when: str) -> None:
    """②を送った日を記録する。**生涯1回なので、これが立てば二度と送らない。**"""
    with get_connection() as conn:
        conn.execute("UPDATE watches SET notified_rise_at = ? WHERE id = ?",
                     (when, watch_id))


def mark_subscription_sent(subscription_id: int, when: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE push_subscriptions SET last_sent_at = ? WHERE id = ?",
            (when, subscription_id))


def insert_notification_log(check_date: str, summary: dict) -> None:
    """通知ジョブ1回分を記録する。**KPI の送信数はここからしか出せない。**"""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO notification_logs
                (check_date, watches_checked, low_triggered, rise_triggered,
                 sent, gone, failed, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (check_date, summary.get("checked", 0), summary.get("low", 0),
             summary.get("rise", 0), summary.get("sent", 0),
             summary.get("gone", 0), summary.get("failed", 0), _now()),
        )


def push_kpi(check_date: str) -> dict:
    """通知まわりの KPI。

    購読数は「いまの状態」、送信数は「その日の記録」。同じ日に複数回
    走ったら合算する（手で走らせることがある）。
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM push_subscriptions)                 AS subscriptions,
              (SELECT COUNT(DISTINCT watch_id)
                 FROM push_subscription_watches)                        AS watches_with_push,
              (SELECT COALESCE(SUM(sent), 0) FROM notification_logs
                 WHERE check_date = ?)                                  AS sent,
              (SELECT COALESCE(SUM(gone), 0) FROM notification_logs
                 WHERE check_date = ?)                                  AS gone
            """,
            (check_date, check_date),
        ).fetchone()
    return dict(row)


# ============================================================
# 予測の答え合わせ（課題9）
# ============================================================

def score_predictions(today: str) -> int:
    """対象日を迎えた予測に、その日の実測を書き込む。**冪等。**

    突き合わせは**同じ日・同じ便**で行う。便が違えば別の量なので、
    経由回数・経由地・航空会社まで一致させる。

    その日その便が観測されなかった予測は NULL のまま残す。**0 で埋めない** —
    「外した」と「比べる相手がいない」は違う。前者だけを精度に数える。

    戻り値は新たに採点した件数。
    """
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE prediction_log
            SET actual_usd = (
                    SELECT s.price_usd FROM snapshots s
                    WHERE s.check_date     = prediction_log.target_check_date
                      AND s.departure_date = prediction_log.departure_date
                      AND s.return_date    = prediction_log.return_date
                      AND s.origin         = prediction_log.origin
                      AND s.destination    = prediction_log.destination
                      AND s.stops          = prediction_log.stops
                      AND s.via_airport    = prediction_log.via_airport
                      AND s.airline        = prediction_log.airline
                      AND s.departure_time = prediction_log.departure_time
                      AND s.source = 'real'),
                scored_at = ?
            WHERE actual_usd IS NULL
              AND target_check_date <= ?
              AND EXISTS (
                    SELECT 1 FROM snapshots s
                    WHERE s.check_date     = prediction_log.target_check_date
                      AND s.departure_date = prediction_log.departure_date
                      AND s.return_date    = prediction_log.return_date
                      AND s.origin         = prediction_log.origin
                      AND s.destination    = prediction_log.destination
                      AND s.stops          = prediction_log.stops
                      AND s.via_airport    = prediction_log.via_airport
                      AND s.airline        = prediction_log.airline
                      AND s.departure_time = prediction_log.departure_time
                      AND s.source = 'real')
            """,
            (_now(), today),
        )
    return cur.rowcount


def prediction_scores(since_target_date: str) -> list[dict]:
    """採点済みの予測を返す。集計は呼び出し側で行う。

    中央値を SQL で出せないので生の行を返す。件数は日に数百のオーダー。
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT predicted_on, target_check_date, departure_date, return_date,
                   origin, destination, airline,
                   predicted_usd, lower_usd, upper_usd, actual_usd,
                   observed_points,
                   CAST(julianday(target_check_date) - julianday(predicted_on)
                        AS INTEGER) AS lead_days,
                   CAST(julianday(departure_date) - julianday(target_check_date)
                        AS INTEGER) AS days_before_departure
            FROM prediction_log
            WHERE actual_usd IS NOT NULL
              AND actual_usd > 0
              AND target_check_date >= ?
            ORDER BY target_check_date
            """,
            (since_target_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_scored_predictions() -> int:
    with get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM prediction_log "
            "WHERE actual_usd IS NOT NULL").fetchone()["c"]

# ----------------------------------------------------------------------
# 運用ダッシュボード用の集計（2026-09-01）
# ----------------------------------------------------------------------

def largest_drops(limit: int = 10, min_points: int = 5) -> list[dict]:
    """モニター開始時から最も下がった系列を返す。

    **「その便が安くなった」ではない。** 追っているのは「その日その区間で
    買える最安値」で、便も販売者も入れ替わる（課題1）。下落幅が大きい
    ものほど、同じ便の値下がりではなく安い便の出現である可能性が高い。
    画面側でその旨を必ず添えること。

    観測が min_points 未満の系列は外す。2〜3点では「開始時」がただの
    外れ値でも下落率が大きく出てしまう。
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT origin, destination, departure_date, return_date,
                   check_date, MIN(price_usd) AS px
            FROM snapshots WHERE source = 'real'
            GROUP BY origin, destination, departure_date, return_date, check_date
            ORDER BY check_date
            """
        ).fetchall()

    series: dict[tuple, list] = {}
    for r in rows:
        key = (r["origin"], r["destination"], r["departure_date"], r["return_date"])
        series.setdefault(key, []).append((r["check_date"], r["px"]))

    today = date.today()
    out = []
    for (o, d, dep, ret), pts in series.items():
        if len(pts) < min_points:
            continue
        start_day, start = pts[0]
        if not start:
            continue
        low_day, low = min(pts[1:], key=lambda p: p[1])
        cur_day, cur = pts[-1]
        drop = (low - start) / start * 100
        if drop >= 0:
            continue
        dd = date.fromisoformat(dep)
        out.append({
            "leg": f"{o}-{d}", "dep": dep, "rt": bool(ret), "n": len(pts),
            "start": round(start), "low": round(low), "cur": round(cur),
            "drop_pct": round(drop, 1),
            "cur_pct": round((cur - start) / start * 100, 1),
            "low_dbd": (dd - date.fromisoformat(low_day)).days,
            "start_dbd": (dd - date.fromisoformat(start_day)).days,
            "done": dd < today,
        })
    out.sort(key=lambda r: r["drop_pct"])
    return out[:limit]


def ops_meta() -> dict:
    """ダッシュボードの見出しに出す現在値。**個人に紐づく値は返さない。**"""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM watches WHERE purpose = 'user')        AS total,
              (SELECT COUNT(DISTINCT created_from_ip_hash) FROM watches
                WHERE purpose = 'user')                                    AS creators,
              (SELECT COUNT(*) FROM monitored_routes WHERE active = 1)     AS routes,
              (SELECT COUNT(*) FROM snapshots WHERE source = 'real')       AS recs,
              (SELECT COUNT(*) FROM push_subscriptions)                    AS subs,
              (SELECT COUNT(*) FROM (SELECT DISTINCT origin, destination,
                 departure_date FROM snapshots
                 WHERE source = 'real' AND departure_date < date('now')))  AS done
            """
        ).fetchone()
    m = dict(row)
    with get_connection() as conn:
        counted = conn.execute(
            """
            SELECT COUNT(*) AS n FROM (
              SELECT origin, destination, departure_date, return_date
              FROM snapshots WHERE source = 'real'
              GROUP BY origin, destination, departure_date, return_date
              HAVING COUNT(DISTINCT check_date) >= 5)
            """
        ).fetchone()
    m["series"] = counted["n"]
    m["dropped"] = len(largest_drops(limit=100000, min_points=5))
    # 稼働ルートのうち、調査用のモニターからしか参照されていない本数。
    # **画面の「54% は調査用」を焼き込まないため。** 数字を文言に固めると
    # 静かに古くなる（2026-09-10 に、8月末の値のまま出ていることに気づいた）。
    with get_connection() as conn:
        research = conn.execute(
            "SELECT COUNT(*) AS n FROM monitored_routes r "
            "WHERE r.active = 1 "
            "  AND EXISTS (SELECT 1 FROM watch_routes wr JOIN watches w "
            "                ON w.id = wr.watch_id "
            "              WHERE wr.monitored_route_id = r.id "
            "                AND w.purpose = 'research') "
            "  AND NOT EXISTS (SELECT 1 FROM watch_routes wr JOIN watches w "
            "                    ON w.id = wr.watch_id "
            "                  WHERE wr.monitored_route_id = r.id "
            "                    AND w.purpose = 'user')"
        ).fetchone()
    m["research_routes"] = research["n"]
    # 稼働ルートの上限。画面の「/1000」を焼き込まないため（2026-10-15 に
    # 2000 へ上げたとき、画面だけ古い値のまま残るところだった）。
    from app.config import get_settings
    m["route_cap"] = get_settings().max_active_routes
    return m



# ============================================================
# 買い時の判定の記録
# ============================================================

def upsert_verdict(logged_on: str, origin: str, destination: str,
                   departure_date: str, days_before_departure: int,
                   verdict: str, basis: Optional[str] = None,
                   observed_days: Optional[int] = None,
                   current_price_usd: Optional[float] = None,
                   predicted_min_usd: Optional[float] = None,
                   predicted_min_upper_usd: Optional[float] = None,
                   predicted_min_date: Optional[str] = None,
                   rise_date: Optional[str] = None,
                   return_date: str = "",
                   ml_drop_prob: Optional[float] = None,
                   ml_model: Optional[str] = None) -> None:
    """その日の判定を1件残す。**同じ日に流し直したら上書き**（二重に数えない）。"""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO verdict_log
                (logged_on, origin, destination, departure_date, return_date,
                 days_before_departure, verdict, basis, observed_days,
                 current_price_usd, predicted_min_usd, predicted_min_upper_usd,
                 predicted_min_date, rise_date, ml_drop_prob, ml_model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (logged_on, origin, destination, departure_date, return_date)
            DO UPDATE SET days_before_departure   = excluded.days_before_departure,
                          verdict                 = excluded.verdict,
                          basis                   = excluded.basis,
                          observed_days           = excluded.observed_days,
                          current_price_usd       = excluded.current_price_usd,
                          predicted_min_usd       = excluded.predicted_min_usd,
                          predicted_min_upper_usd = excluded.predicted_min_upper_usd,
                          predicted_min_date      = excluded.predicted_min_date,
                          rise_date               = excluded.rise_date,
                          ml_drop_prob            = excluded.ml_drop_prob,
                          ml_model                = excluded.ml_model
            """,
            (logged_on, origin, destination, departure_date, return_date,
             days_before_departure, verdict, basis, observed_days,
             current_price_usd, predicted_min_usd, predicted_min_upper_usd,
             predicted_min_date, rise_date, ml_drop_prob, ml_model, _now()),
        )


def one_way_daily_rows(departed_before: Optional[str] = None,
                       departing_from: Optional[str] = None
                       ) -> dict[tuple[str, str, str], dict[int, tuple]]:
    """片道の各便について、日ごとの (最安, 航空会社の数, 直行の最安)。

    影の機械学習（app/ml/drop_model.py）の材料。**自前の実測（snapshots）だけ。**
    departed_before を渡すと出発日がそれより前の便（学習用）、
    departing_from を渡すとその日以降に出発する便（今日の予測用）に絞る。
    """
    where, args = ["source = 'real'", "return_date = ''"], []
    if departed_before:
        where.append("departure_date < ?"); args.append(departed_before)
    if departing_from:
        where.append("departure_date >= ?"); args.append(departing_from)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT origin, destination, departure_date, check_date,
                   MIN(price_usd) AS p, COUNT(DISTINCT airline) AS n_air,
                   MIN(CASE WHEN stops = 0 THEN price_usd END) AS p_nonstop
            FROM snapshots WHERE {' AND '.join(where)}
            GROUP BY origin, destination, departure_date, check_date
            """,
            args,
        ).fetchall()
    out: dict[tuple[str, str, str], dict[int, tuple]] = {}
    for r in rows:
        dbd = (date.fromisoformat(r["departure_date"]) - date.fromisoformat(r["check_date"])).days
        if dbd < 0:
            continue
        out.setdefault((r["origin"], r["destination"], r["departure_date"]), {})[dbd] = (
            r["p"], r["n_air"], r["p_nonstop"])
    return out


def completed_one_way_price_paths(today: str) -> dict[tuple[str, str, str], dict[int, float]]:
    """出発日を迎えた片道の系列について、日ごとの最安値の推移。

    戻り値は (出発地, 到着地, 出発日) -> {出発までの日数: その日の最安}。

    **我々が毎朝見た最安（snapshots）だけを使う。price_history は混ぜない。**
    2026-10-12 の初版は両方を合わせ、同じ日に両方あれば安いほうを採っていた。
    2026-10-14、重なった 1,123 日で突き合わせると一致（±1%）は 40% だけで、
    **59% の日で price_history のほうが低かった**（中央値 -2.4%、高いのは 1%）。
    取得元の過去価格は、我々の取り方では拾えない運賃まで含む別の量らしい。

    price_history が入るのは主に出発から遠い日（取り込みが 2026-08-20 まで）
    なので、混ぜると**遠い日が系統的に安く見え、「早く買うほど底値に近い」が
    強く出る**。買い時の答え合わせはまさにその比較なので、片方だけで測る。

    底値の当否を測るための材料（app/buy_timing.py）。
    """
    paths: dict[tuple[str, str, str], dict[int, float]] = {}
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT origin, destination, departure_date, check_date AS day,
                   MIN(price_usd) AS price
            FROM snapshots
            WHERE source = 'real' AND return_date = '' AND departure_date < ?
            GROUP BY origin, destination, departure_date, check_date
            """,
            (today,),
        ).fetchall()
    for r in rows:
        dbd = (date.fromisoformat(r["departure_date"]) - date.fromisoformat(r["day"])).days
        if dbd < 0:
            continue
        key = (r["origin"], r["destination"], r["departure_date"])
        paths.setdefault(key, {})[dbd] = r["price"]
    return paths


def verdicts_for_departed_routes(today: str) -> list[dict]:
    """出発日を迎えたルートの判定の記録。答え合わせの材料。"""
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT logged_on, origin, destination, departure_date,
                   days_before_departure, verdict, current_price_usd,
                   predicted_min_date, predicted_min_usd
            FROM verdict_log
            WHERE return_date = '' AND departure_date < ?
            ORDER BY origin, destination, departure_date, logged_on
            """,
            (today,),
        )]


def first_verdict_date() -> Optional[str]:
    """判定の記録を始めた日。まだ無ければ None。"""
    with get_connection() as conn:
        row = conn.execute("SELECT MIN(logged_on) AS d FROM verdict_log").fetchone()
    return row["d"] if row else None
