"""SQLite コネクション管理。

SQL はここには書かない — repository.py に閉じ込める。
将来 Postgres へ移す際、差し替えるのはこの2ファイルだけで済むようにする。
"""
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import get_settings

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _db_path() -> Path:
    return Path(get_settings().db_path)


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """トランザクション境界つきのコネクション。

    正常終了で commit、例外で rollback。呼び出し側で commit を書かない。
    """
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # 外部キーはデフォルト無効。watch_routes の ON DELETE CASCADE が
    # 効かなくなるので、接続ごとに必ず有効化する。
    conn.execute("PRAGMA foreign_keys = ON")
    # 収集ジョブ（書き込み）と Web リクエスト（読み取り）が並行するため。
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 既存 DB に後から足したカラム。
#
# schema.sql は CREATE TABLE IF NOT EXISTS なので、既にテーブルがある DB には
# 新しいカラムが入らない。稼働中の本番 DB を作り直さずに済ませるため、
# 「無ければ ALTER TABLE で足す」だけの最小限の仕組みを持つ。
#
# NOT NULL にするなら DEFAULT が要る（SQLite の ALTER TABLE ADD COLUMN の
# 制約）。NULL 許容ならそのまま足せる。それ以上のことが必要になったら、
# 正式なマイグレーションツールを入れる。
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    # (テーブル, カラム, 定義)
    ("watches", "nearby_airports", "INTEGER NOT NULL DEFAULT 1"),
    ("watches", "max_total_minutes", "INTEGER"),
    # 調査用モニターを KPI の需要・定着の列から外すために足した。
    # **既存行は既定の 'user' になる。** それが正しい（利用者のもの）。
    ("watches", "purpose", "TEXT NOT NULL DEFAULT 'user'"),
    # 復路の日付。**'' が片道。** watches と fetch_errors は UNIQUE に
    # 日付を含まないので ALTER で足せる。他の4テーブルは足せない
    # （下の _rebuild_for_return_date を見よ）。
    ("watches", "return_date", "TEXT NOT NULL DEFAULT ''"),
    # 通知の送信済み状態（課題4）。既存行は NULL = まだ何も送っていない。
    ("push_subscriptions", "lang", "TEXT NOT NULL DEFAULT 'en'"),
    # 予測の答え合わせ（課題9）。既存行は NULL = 未採点。
    ("prediction_log", "actual_usd", "REAL"),
    ("prediction_log", "scored_at", "TEXT"),
    ("watches", "notified_low_usd", "REAL"),
    ("watches", "notified_rise_at", "TEXT"),
    ("fetch_errors", "return_date", "TEXT NOT NULL DEFAULT ''"),
    ("monitored_routes", "disabled_at", "TEXT"),
    ("monitored_routes", "disabled_reason", "TEXT"),
    ("monitored_routes", "disabled_kind", "TEXT"),
    # 便の詳細。取得済みのものを捨てていたので後から足した。
    # 既存行は NULL のまま（遡って取得できない）。
    ("snapshots", "departure_time", "TEXT"),
    ("snapshots", "arrival_time", "TEXT"),
    ("snapshots", "air_minutes", "INTEGER"),
    ("snapshots", "total_minutes", "INTEGER"),
    ("snapshots", "plane_types", "TEXT"),
    ("snapshots", "carbon_g", "INTEGER"),
    # 0件の理由。収集中にだけ0件が増える現象（2026-10）の切り分けのため。
    # 既存行は NULL のまま（遡って分からない）。
    ("empty_searches", "reason", "TEXT"),
    # 影の機械学習（2026-10-23）。既存行は NULL = その日はまだ動いていなかった。
    ("verdict_log", "ml_drop_prob", "REAL"),
    ("verdict_log", "ml_model", "TEXT"),
]


def _apply_column_migrations(conn: sqlite3.Connection) -> list[str]:
    applied = []
    for table, column, definition in _ADDED_COLUMNS:
        existing = {
            r["name"] for r in conn.execute(f"PRAGMA table_info({table})")
        }
        if not existing:
            continue        # テーブル自体がまだ無い（新規 DB）
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            applied.append(f"{table}.{column}")
    return applied


# 「列を足すだけ」では済まないテーブル。
#
# **SQLite はテーブル定義内の UNIQUE を後から変えられない。** UNIQUE は
# sqlite_autoindex_* という暗黙インデックスになり、DROP も ALTER もできない。
# return_date を UNIQUE に含めないと、同じ区間・同じ出発日の片道と往復が
# 同一行と判定され、**先に入っていたほうが黙って上書きされる**（実測で確認）。
#
#     片道 NRT->LAX 11/10 ANA $2,164 (return_date='')
#     往復 NRT->LAX 11/10 ANA $2,226 (return_date='2026-11-20')
#     → INSERT OR REPLACE で残る行は1つ
#
# 金額が2倍近く違うので、混ざれば較正と買い時判定が静かに狂う。
# したがって作り直す以外にない。
_REBUILT_TABLES = ("monitored_routes", "snapshots", "prediction_log",
                   "price_history")

_CREATE_RE = r"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\);"


def _create_table_sql(schema: str, table: str) -> str:
    """schema.sql から1テーブル分の CREATE 文を取り出す。"""
    match = re.search(_CREATE_RE.format(table=table), schema, re.S)
    if not match:
        raise RuntimeError(f"schema.sql に {table} の CREATE 文が見つからない")
    return f"CREATE TABLE {table}__new (\n{match.group(1)}\n);"


def _rebuild_for_return_date() -> list[str]:
    """UNIQUE に return_date を含めるため、4テーブルを作り直す。

    **冪等。** return_date を既に持つテーブルは触らない。新規 DB では
    schema.sql が正しい定義で作るので、ここは何もしない。

    SQLite 公式の手順（新テーブル作成 → コピー → 差し替え）に従う。
    get_connection を使わず自前で繋ぐのは、PRAGMA foreign_keys と
    legacy_alter_table が**トランザクションの中では変えられない**ため。

    monitored_routes だけは watch_routes から参照されている。id を保ったまま
    コピーするので参照は保たれるが、作業中は外部キーを切り、終わったあとに
    foreign_key_check で確かめる。legacy_alter_table を立てるのは、
    RENAME 時に SQLite が他テーブルの REFERENCES を書き換えようとして
    「no such table」で落ちるのを避けるため。
    """
    path = _db_path()
    if not path.exists():
        return []

    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        todo = []
        for table in _REBUILT_TABLES:
            columns = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            if not columns:
                continue            # テーブルがまだ無い（新規 DB）
            if "return_date" not in columns:
                # **新しい定義で作り直すので、新しい制約に合わせて詰める。**
                # departure_time は NOT NULL になった（下の
                # _rebuild_for_departure_time を見よ）。NULL のまま流し込むと
                # ここで落ちる。
                reads = {"departure_time": "COALESCE(departure_time, '')"} \
                    if "departure_time" in columns else {}
                todo.append((table, columns, reads))
        if not todo:
            return []

        _copy_into_new_tables(conn, schema, todo)
    finally:
        conn.close()

    return [t for t, _, _ in todo]


def _copy_into_new_tables(conn, schema: str, todo) -> None:
    """新定義でテーブルを作り直し、中身を移す。**1件でも落ちたら巻き戻す。**

    `todo` は (テーブル名, 移す列, 読み替え) の並び。読み替えは
    「新しい列 -> 読み出す式」で、既定は同名の列をそのまま。列の型や
    NULL 可否が変わるときにここで詰める（例: departure_time の NULL -> ''）。

    PRAGMA はトランザクションの外でしか効かないので、BEGIN の前に立てる。
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("BEGIN")
    try:
        for table, columns, *rest in todo:
            reads = rest[0] if rest else {}
            before = conn.execute(
                f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            conn.execute(_create_table_sql(schema, table))
            names = ", ".join(columns)
            values = ", ".join(reads.get(c, c) for c in columns)
            conn.execute(
                f"INSERT INTO {table}__new ({names}) SELECT {values} FROM {table}")
            after = conn.execute(
                f"SELECT COUNT(*) AS c FROM {table}__new").fetchone()["c"]
            if before != after:
                # 1件でも落ちたら全部巻き戻す。
                raise RuntimeError(
                    f"{table} の移送で件数が変わった: {before} -> {after}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")

        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"外部キーが壊れた: {[tuple(v) for v in violations]}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def _rebuild_for_departure_time() -> list[str]:
    """UNIQUE に departure_time を含めるため、2テーブルを作り直す。

    **同じ社・同じ経由地で1日に何本も飛ぶ。** 2026-09-08 の SAN→HND は
    United+ANA・LAX 経由が 06:15 と 17:00 の2本あった。departure_time が
    鍵に入っていないと2本が1行を奪い合い、`_cheapest_distinct` が安いほうを
    選ぶので、**日によって中身の違う便が1本の線につながる。** 実際
    8/23〜8/27 に $688 で出ていたのは、それ以前の $588 とは別の便だった。

    既存行の NULL は '' にする。SQLite の UNIQUE は NULL != NULL と評価
    するので、NULL のまま鍵に入れると重複排除が黙って壊れる（via_airport で
    同じ轍を踏んでいる）。'' は「時刻が取れなかった便」であって、時刻の
    違う便が1つにまとまるが、**遡って取れないので他に選びようがない。**
    列を足す前の行（2026-08-10 以前）と、正時の時刻を落としていた行
    （2026-09-08 に修正）がここに入る。

    **冪等。** departure_time が既に NOT NULL のテーブルは触らない。
    """
    path = _db_path()
    if not path.exists():
        return []

    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        todo = []
        for table in ("snapshots", "prediction_log"):
            info = {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})")}
            if not info:
                continue                        # テーブルがまだ無い
            current = info.get("departure_time")
            if current is not None and current["notnull"]:
                continue                        # 既に鍵に入っている
            columns = list(info)
            reads = {}
            if current is None:
                # prediction_log には列そのものが無い。**足しつつ作り直す。**
                columns.append("departure_time")
                reads["departure_time"] = "''"
            else:
                reads["departure_time"] = "COALESCE(departure_time, '')"
            todo.append((table, columns, reads))
        if not todo:
            return []

        _copy_into_new_tables(conn, schema, todo)
    finally:
        conn.close()

    return [t for t, _, _ in todo]


def _backfill_prediction_departure_time(conn) -> int:
    """移行で '' になった予測に、その予測が指していた便の出発時刻を書く。

    **これを忘れると、採点待ちの予測が二度と採点されない。** 2026-09-08 に
    departure_time を突き合わせの条件に足したので、予測側が '' のままだと
    実時刻を持つスナップショットと一致しなくなる。移行の時点で
    prediction_log は 39,035 件あり、うち 26,000 件ほどが未採点だった。

    埋め方は一意に決まる。予測は「その日の実測から作った便」についてのもので
    （engine._log_predictions が `cheapest` を使う）、移行前は
    **1日・1便あたり1行しか持てなかった**。したがって predicted_on の日の
    同じ (経由回数, 経由地, 航空会社) のスナップショットは高々1行で、
    その出発時刻がその予測の便の出発時刻になる。

    対応するスナップショットが無い予測（転用や曲線だけを根拠にしたもの）は
    '' のまま残す。**推測して埋めない** — 間違った便で採点するくらいなら、
    採点しないほうがよい。

    冪等。既に値が入っている行は対象外。
    """
    cur = conn.execute(
        """
        UPDATE prediction_log
        SET departure_time = (
                SELECT s.departure_time FROM snapshots s
                WHERE s.check_date     = prediction_log.predicted_on
                  AND s.departure_date = prediction_log.departure_date
                  AND s.return_date    = prediction_log.return_date
                  AND s.origin         = prediction_log.origin
                  AND s.destination    = prediction_log.destination
                  AND s.stops          = prediction_log.stops
                  AND s.via_airport    = prediction_log.via_airport
                  AND s.airline        = prediction_log.airline
                  AND s.source = 'real')
        WHERE departure_time = ''
          AND EXISTS (
                SELECT 1 FROM snapshots s
                WHERE s.check_date     = prediction_log.predicted_on
                  AND s.departure_date = prediction_log.departure_date
                  AND s.return_date    = prediction_log.return_date
                  AND s.origin         = prediction_log.origin
                  AND s.destination    = prediction_log.destination
                  AND s.stops          = prediction_log.stops
                  AND s.via_airport    = prediction_log.via_airport
                  AND s.airline        = prediction_log.airline
                  AND s.source = 'real'
                  AND s.departure_time <> '')
        """
    )
    return cur.rowcount


def _backfill_nonstop_duration(conn) -> int:
    """直行便の総所要を飛行時間から埋める。

    total_minutes は収集時にしか計算できない（区間の内訳を保存していない）。
    ただし**直行便に限れば乗継が無いので、総所要 = 飛行時間**で確定する。
    1経由以上は埋められないので触らない。

    冪等。既に値が入っている行は対象外。
    """
    cur = conn.execute(
        """
        UPDATE snapshots SET total_minutes = air_minutes
        WHERE total_minutes IS NULL AND air_minutes IS NOT NULL AND stops = 0
        """
    )
    return cur.rowcount


def init_db() -> Path:
    """スキーマを適用する。冪等（全て IF NOT EXISTS）。"""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    # **executescript より先に。** CREATE TABLE IF NOT EXISTS は既存テーブルを
    # 作り直さないので、UNIQUE の変更はここでしか入らない。再構築でインデックスは
    # 消えるが、直後の executescript が CREATE INDEX IF NOT EXISTS で戻す。
    _rebuild_for_return_date()
    # **return_date の作り直しより後。** 先に走らせると、まだ古い定義の
    # テーブルを新しい定義で作り直そうとして列が食い違う。
    _rebuild_for_departure_time()
    with get_connection() as conn:
        conn.executescript(schema)
        _apply_column_migrations(conn)
        _backfill_nonstop_duration(conn)
        _backfill_prediction_departure_time(conn)
    return _db_path()


def table_names() -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]
