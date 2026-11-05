-- 航空チケット価格予測アプリ — スキーマ
--
-- 設計上の要は「ユーザー視点のモニター(watches)」と「システム視点の収集対象
-- (monitored_routes)」を分離していること。複数ユーザーが同じルート・同じ出発日を
-- 登録しても価格取得は1回に集約され、API コストがユーザー数ではなく
-- 異なるルート数に比例する。

PRAGMA foreign_keys = ON;


-- ============================================================
-- 1. monitored_routes — 収集対象（システム視点）
--    日次収集ジョブはこのテーブルだけを見る。
-- ============================================================
-- ============================================================
-- 一覧（collections）
--
-- モニターの URL を1本にまとめるための入れ物。localStorage は
-- 「サイトデータを削除」で消えるため、複数のモニターを持つ利用者が
-- 索引ごと失う経路があった。Cookie も IndexedDB も同じ操作で消えるので、
-- 端末の外に置く以外に手段がない。
--
-- **モニターと同じ capability として扱う。** 一覧のトークンを知っている
-- =その一覧を開ける。認証は無い。扱うのが航空券の価格と日付だけで、
-- 個人情報と結びつかないため、利便性を優先した判断（2026-08-11）。
-- ============================================================

CREATE TABLE IF NOT EXISTS collections (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    token            TEXT    NOT NULL UNIQUE,
    created_from_ip_hash TEXT,
    created_at       TEXT    NOT NULL,
    last_accessed_at TEXT    NOT NULL,
    -- 中身より先に消えると、一覧を失うという当初の問題がそのまま起きる。
    -- 所属モニターの最長の有効期限に合わせて伸ばす。
    expires_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_watches (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    -- モニターが消えたら一覧からも自動で落ちる。
    watch_id      INTEGER NOT NULL REFERENCES watches(id)     ON DELETE CASCADE,
    added_at      TEXT    NOT NULL,
    PRIMARY KEY (collection_id, watch_id)
);

CREATE INDEX IF NOT EXISTS idx_collection_watches_watch
    ON collection_watches (watch_id);


-- 為替レート。日本語表示のときの換算にだけ使う。
-- 収集も予測も USD のまま行う（換算は表示の最後だけ）。
CREATE TABLE IF NOT EXISTS fx_rates (
    pair       TEXT NOT NULL,          -- 'USD/JPY'
    rate_date  TEXT NOT NULL,          -- 公表日（ECB は平日のみ）
    rate       REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (pair, rate_date)
);


CREATE TABLE IF NOT EXISTS monitored_routes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    origin            TEXT    NOT NULL,
    destination       TEXT    NOT NULL,
    departure_date    TEXT    NOT NULL,          -- YYYY-MM-DD
    -- 復路の日付。**'' が片道。** NULL にしない（下の UNIQUE が壊れる）。
    return_date       TEXT    NOT NULL DEFAULT '',
    active            INTEGER NOT NULL DEFAULT 1,
    first_seen_at     TEXT    NOT NULL,
    last_collected_at TEXT,
    -- 収集し続けても何も取れないルートを止めた記録。
    -- 空港は実在するのに毎回パーサが落ちる組み合わせ（ARN-NRT など）と、
    -- 検証を入れる前に登録された実在しない空港（NAR / MIL / CST）が該当する。
    -- 出発日まで毎日失敗し続け、ルート枠と日次リクエストを食う。
    disabled_at       TEXT,
    disabled_reason   TEXT,
    -- 停止の種類。**画面の文言が変わるので、日本語の理由文を解析させない。**
    -- 'no_flights'（便が無い＝運航されていない疑い）/ 'fetch_failed'（取れない）
    disabled_kind     TEXT,
    UNIQUE (origin, destination, departure_date, return_date)
);

CREATE INDEX IF NOT EXISTS idx_routes_active
    ON monitored_routes (active, departure_date);


-- ============================================================
-- 2. watches — ユーザー視点のモニター（匿名）
--    token が実質のアカウント。URL を知っている = 閲覧・編集できる
--    (capability) という前提で運用する。
-- ============================================================
CREATE TABLE IF NOT EXISTS watches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 推測不可能な 128bit 相当のトークン。連番 ID は不可。
    token            TEXT    NOT NULL UNIQUE,
    origin           TEXT    NOT NULL,
    destination      TEXT    NOT NULL,
    departure_date   TEXT    NOT NULL,
    -- 復路の日付。**'' が片道。** 既存のモニターは全てこれになる。
    return_date      TEXT    NOT NULL DEFAULT '',
    -- 航空会社フィルター。JSON 配列（空配列 = 絞り込みなし）。
    airline_filter   TEXT    NOT NULL DEFAULT '[]',
    -- 総所要時間の上限（分）。NULL = 上限なし。
    -- 乗継が一晩になる便は、最安でも候補にならない（実測: NRT-YYC-SAN が
    -- $799 で総所要 33h10、うち乗継 20h35）。表示だけでなく較正と
    -- 買い時判定の入力からも外す。
    max_total_minutes INTEGER,
    -- 近接空港ルール（2.3）を使うか。1 = 使う。
    -- 切ると expand_routes が指定どおりのルートだけを返す。
    -- 既定はオフ（2026-08-14 に変更）。設定タブで各自が有効にする。
    -- **既存の watches は 1 のまま。** 既定を変えたからといって、
    -- すでに監視している区間を黙って止めてはいけない。
    nearby_airports  INTEGER NOT NULL DEFAULT 0,
    -- 何のために作られたモニターか。
    --   'user'     … 利用者が登録したもの
    --   'research' … 我々が価格曲線を調べるために作ったもの
    --
    -- **KPI の需要・定着の列から research を外すために要る。** 2026-08-17 に
    -- 調査用モニターを120件入れた。区別しないと「作られたきり開かれていない
    -- モニターが何件あるか」（前日 73/110）が自分たちの分で埋まり、利用者が
    -- 使っているかどうかを測れなくなる。
    --
    -- 収集の対象としては区別しない。ルートを消費し収集時間を使うのは同じ
    -- なので、容量の列（routes_active）には両方を数える。
    purpose          TEXT    NOT NULL DEFAULT 'user'
                     CHECK (purpose IN ('user', 'research')),
    -- Phase 4 以降の任意メール通知用。現時点では常に NULL。
    -- カラムだけ先に用意しておき、後からマイグレーションを打たずに済ませる。
    notify_email     TEXT,
    -- 通知の送信済み状態（課題4）。**同じことを二度送らないために要る。**
    --   notified_low_usd … ①で最後に知らせた最安。これを下回ったときだけ送る
    --   notified_rise_at … ②（出発30日前）を送った日。生涯1回
    notified_low_usd REAL,
    notified_rise_at TEXT,
    -- レート制限用。生 IP は保存せず、ソルト付きハッシュのみ保持する。
    created_from_ip_hash TEXT,
    created_at       TEXT    NOT NULL,
    last_accessed_at TEXT    NOT NULL,
    expires_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_watches_expires
    ON watches (expires_at);
CREATE INDEX IF NOT EXISTS idx_watches_ip_created
    ON watches (created_from_ip_hash, created_at);


-- ============================================================
-- 3. watch_routes — watches と monitored_routes の多対多
--
--    「多対一」ではない点に注意。2.3 の近接空港ルールにより、
--    1件の watch (HND->SAN) が2件の monitored_routes
--    (HND-SAN, HND-LAX) を必要とするため。
--
--    どの watch からも参照されなくなった monitored_route を
--    止める処理が、この表への外部キーだけで完結する。
-- ============================================================
CREATE TABLE IF NOT EXISTS watch_routes (
    watch_id           INTEGER NOT NULL REFERENCES watches(id)           ON DELETE CASCADE,
    monitored_route_id INTEGER NOT NULL REFERENCES monitored_routes(id)  ON DELETE CASCADE,
    PRIMARY KEY (watch_id, monitored_route_id)
);

CREATE INDEX IF NOT EXISTS idx_watch_routes_route
    ON watch_routes (monitored_route_id);


-- ============================================================
-- 4. snapshots — 日次の価格スナップショット
--    既存 flight-price-monitor/db.py の設計を継承。
--
--    monitored_routes への外部キーを意図的に張っていない。
--    モニターが消えても収集済みの価格履歴は資産として残し、
--    同じルートを誰かが再登録したとき即座に過去トレンドを出せるようにする。
-- ============================================================
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    check_date     TEXT    NOT NULL,             -- 価格を取得した日
    departure_date TEXT    NOT NULL,
    -- 復路の日付。**'' が片道**（NULL にしない）。
    -- via_airport と同じ理由: SQLite の UNIQUE は NULL != NULL と評価するので、
    -- NULL 許容にすると片道行の重複排除が黙って壊れる。
    return_date    TEXT    NOT NULL DEFAULT '',
    origin         TEXT    NOT NULL,
    destination    TEXT    NOT NULL,
    stops          INTEGER NOT NULL,
    -- via_airport は NULL ではなく '' をデフォルトにする。
    -- SQLite の UNIQUE 制約は NULL != NULL と評価するため、NULL のままだと
    -- nonstop 行（経由地なし）の重複排除が黙って壊れる。
    via_airport    TEXT    NOT NULL DEFAULT '',
    airline        TEXT    NOT NULL,
    price_usd      REAL    NOT NULL,
    -- 便の詳細。fast_flights が返しているのに捨てていた項目。
    -- 後から遡って取得できない。
    --
    -- **departure_time は便の身元の一部。** 同じ社・同じ経由地で1日に何本も
    -- 飛ぶ（2026-09-08 の SAN→HND は United+ANA・LAX 経由が 06:15 と 17:00 の
    -- 2本）。これを鍵に入れないと2本が1行を奪い合い、安いほうだけが残って
    -- **日によって中身の違う便が1本の線につながる。** 実際 8/23〜8/27 に
    -- $688 で出ていたのは、それ以前の $588 とは別の便だった。
    --
    -- NULL ではなく '' を既定にするのは via_airport と同じ理由。SQLite の
    -- UNIQUE は NULL != NULL と評価するので、NULL を許すと時刻の取れなかった
    -- 行の重複排除が黙って壊れる。
    departure_time TEXT    NOT NULL DEFAULT '',  -- 初区間の出発（現地）
    arrival_time   TEXT,            -- 最終区間の到着
    air_minutes    INTEGER,         -- 各区間の飛行時間の合計（乗継時間は含まない）
    -- 総所要（飛行 + 乗継）。**収集時にしか計算できない。**
    -- arrival_time - departure_time では出せない。現地時刻なので
    -- タイムゾーンと日付変更線をまたぐと逆転する
    -- （NRT 18:35 発 → YYC 12:45 着）。乗継は同一空港内なので引き算が成立し、
    -- 飛行時間の合計に足すことで求める。
    total_minutes  INTEGER,
    plane_types    TEXT,            -- 機材。複数区間は ', ' で連結
    carbon_g       INTEGER,         -- CO2 排出量（g）
    source         TEXT    NOT NULL CHECK (source IN ('real', 'simulated')),
    fetched_at     TEXT    NOT NULL,
    UNIQUE (check_date, departure_date, return_date, origin, destination,
            stops, via_airport, airline, departure_time)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_route_date
    ON snapshots (origin, destination, departure_date, check_date);
CREATE INDEX IF NOT EXISTS idx_snapshots_source
    ON snapshots (source);


-- ============================================================
-- 5. prediction_log — 過去に出した予測の記録
--    「予測値と実際の価格を突き合わせる」ための台帳。
--    Phase 3.5（実測から価格曲線パラメータを推定する）の判断材料になる。
-- ============================================================
CREATE TABLE IF NOT EXISTS prediction_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    predicted_on      TEXT    NOT NULL,          -- 予測を出した日
    target_check_date TEXT    NOT NULL,          -- 予測の対象日
    departure_date    TEXT    NOT NULL,
    -- 復路の日付。'' が片道（snapshots と同じ規約）。
    return_date       TEXT    NOT NULL DEFAULT '',
    origin            TEXT    NOT NULL,
    destination       TEXT    NOT NULL,
    stops             INTEGER NOT NULL,
    via_airport       TEXT    NOT NULL DEFAULT '',
    airline           TEXT    NOT NULL,
    predicted_usd     REAL    NOT NULL,
    -- 信頼帯。予測は必ず区間を持つ（決定論的な1本の線にしない）。
    lower_usd         REAL    NOT NULL,
    upper_usd         REAL    NOT NULL,
    -- 何点の実測で較正したか。区間幅の根拠であり、UI の
    -- 「実測◯日分」ラベルの出どころでもある。
    observed_points   INTEGER NOT NULL,
    -- 答え合わせ（課題9）。対象日を迎えたあとに後から書き込む。
    --
    -- **別表にせず同じ行に入れる。** 予測と結果を離すと、突き合わせ条件
    -- （日付・便・経由地）を書くたびに再現しなければならず、1箇所でも
    -- ずれると静かに違うものを比べる。
    --
    -- NULL = まだ答え合わせしていない（対象日が来ていない、またはその日
    -- その便が観測されなかった）。**観測されなかったことと誤差0を
    -- 区別する必要があるので、0 で埋めない。**
    actual_usd        REAL,
    scored_at         TEXT,
    created_at        TEXT    NOT NULL,
    departure_time    TEXT    NOT NULL DEFAULT '',   -- 便の身元の一部
    -- **予測も便ごと。** snapshots と同じ鍵でないと、答え合わせが
    -- 「同じ社・同じ経由地の別の便」を拾ってしまう。
    UNIQUE (predicted_on, target_check_date, departure_date, return_date,
            origin, destination, stops, via_airport, airline, departure_time)
);

CREATE INDEX IF NOT EXISTS idx_prediction_target
    ON prediction_log (target_check_date, departure_date);


-- ============================================================
-- 6. collection_logs — 収集ジョブ1回分の結果
--    PI-27-flightTrend の schema.sql を踏襲。
-- ============================================================
CREATE TABLE IF NOT EXISTS collection_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    check_date     TEXT    NOT NULL,
    status         TEXT    NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
    source         TEXT    NOT NULL,             -- 'scheduler' | 'manual' | ...
    routes_checked INTEGER NOT NULL DEFAULT 0,
    records_saved  INTEGER NOT NULL DEFAULT 0,
    errors         TEXT,                         -- JSON 配列
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_collection_logs_date
    ON collection_logs (check_date);


-- ============================================================
-- 7. fetch_errors — 個別取得の失敗
--    既存 flight-price-monitor/db.py を継承。日次収集は一部が失敗しても
--    全体をクラッシュさせず、ここに記録して先へ進む。
-- ============================================================
CREATE TABLE IF NOT EXISTS fetch_errors (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    check_date     TEXT    NOT NULL,
    origin         TEXT    NOT NULL,
    destination    TEXT    NOT NULL,
    departure_date TEXT    NOT NULL,
    return_date    TEXT    NOT NULL DEFAULT '',
    stops          INTEGER NOT NULL,
    via_airport    TEXT,
    error_message  TEXT    NOT NULL,
    occurred_at    TEXT    NOT NULL
);

-- 検索は通ったのに便が1件も返らなかった記録。
--
-- **「取れなかった」と「そもそも飛んでいない」は別物。** fetch_errors は
-- 前者しか持たない。運航されていない区間（NRT-GMP など）は例外も出ず
-- 0件が返るだけなので、どこにも記録が残らず、自動停止の判定にも
-- かからないまま「モデル推定」を出し続けてしまう（2026-08-30 の報告）。
--
-- 便が無いことを示す唯一の証拠なので、fetch_errors とは別に持つ。
-- ここを fetch_errors に混ぜると、KPI の誤り件数と collection_status が
-- 「運航されていない区間があるだけ」で悪化して読めなくなる。
CREATE TABLE IF NOT EXISTS empty_searches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    check_date     TEXT    NOT NULL,
    origin         TEXT    NOT NULL,
    destination    TEXT    NOT NULL,
    departure_date TEXT    NOT NULL,
    return_date    TEXT    NOT NULL DEFAULT '',
    stops          INTEGER NOT NULL,
    searched_at    TEXT    NOT NULL,
    -- 0件の理由（2026-10-12 から。それ以前の行は NULL）。
    -- error_status / empty_page / filtered / unknown
    reason         TEXT,
    UNIQUE (check_date, origin, destination, departure_date, return_date, stops)
);

CREATE INDEX IF NOT EXISTS idx_empty_searches_route
    ON empty_searches (origin, destination, departure_date, check_date);

CREATE INDEX IF NOT EXISTS idx_fetch_errors_date
    ON fetch_errors (check_date);


-- ============================================================
-- 12. price_history — Google Flights が応答に載せている過去の価格
--
-- **snapshots とは意味が違うので、混ぜない。**
--   snapshots    … 我々がその日に観測した、便ごとの価格
--   price_history … 取得元が持っていた過去約60日ぶんを、後から写したもの
--
-- 便を特定しない「その検索条件での最安値」なので、便の入れ替わりを含む
-- （実測で日次 5.27% 動く。同一便なら 1.61%）。**金額の精度では snapshots に
-- 劣る。** それでも置く理由は、我々の収集開始前まで遡れること — 底値が
-- 出発何日前に来るかを見るには、この長さのほうが効く。
--
-- captured_at を持つのは、同じ observed_date を別の日に取り直したときに
-- どちらが新しい写しかを判別するため。UNIQUE は「いつの価格か」で張り、
-- 取り直しは上書きする。
-- ============================================================
CREATE TABLE IF NOT EXISTS price_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    origin         TEXT    NOT NULL,
    destination    TEXT    NOT NULL,
    departure_date TEXT    NOT NULL,
    -- 復路の日付。'' が片道（snapshots と同じ規約）。
    return_date    TEXT    NOT NULL DEFAULT '',
    observed_date  TEXT    NOT NULL,             -- その価格が付いていた日
    price_usd      REAL    NOT NULL,
    captured_at    TEXT    NOT NULL,             -- 我々が写した日時
    UNIQUE (origin, destination, departure_date, return_date, observed_date)
);

CREATE INDEX IF NOT EXISTS idx_price_history_route
    ON price_history (origin, destination, departure_date, observed_date);


-- ============================================================
-- 13. push_subscriptions — Web Push の購読（課題4）
--
-- **メールアドレスは受け取らない。** endpoint はプッシュサービス
-- （Google / Mozilla / Apple）が発行する URL で、個人情報ではない。
-- ただし匿名でもない — ブラウザごとに固定で、その端末を継続的に指せる。
-- **仮名**として扱い、画面でその旨を説明する。
--
-- 利用者はブラウザの設定でいつでも切れる。切られた購読は送信時に
-- 404/410 が返るので、そこで消す（こちらから死活監視はしない）。
-- ============================================================
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint     TEXT    NOT NULL UNIQUE,   -- プッシュサービスの URL
    p256dh       TEXT    NOT NULL,          -- 本文の暗号化に使う公開鍵
    auth         TEXT    NOT NULL,          -- 同じく認証用の秘密値
    -- **通知の本文はサーバが作るので、言語を知る必要がある。**
    -- 画面の表示言語を購読時に受け取る。利用者が切り替えたら上書きする。
    lang         TEXT    NOT NULL DEFAULT 'en',
    created_at   TEXT    NOT NULL,
    -- 画面を開くたびに更新する。使われていない購読を掃除する手がかり。
    last_seen_at TEXT    NOT NULL,
    -- 1モニター1日1通の上限に使う。
    last_sent_at TEXT
);

-- どのモニターの通知を受け取りたいか。collections と同じ形にしてある。
CREATE TABLE IF NOT EXISTS push_subscription_watches (
    subscription_id INTEGER NOT NULL
        REFERENCES push_subscriptions(id) ON DELETE CASCADE,
    -- モニターが消えたら購読先からも自動で落ちる。
    watch_id        INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (subscription_id, watch_id)
);

CREATE INDEX IF NOT EXISTS idx_push_sub_watches_watch
    ON push_subscription_watches (watch_id);


-- ============================================================
-- 14. notification_logs — 通知ジョブ1回分の結果（課題4）
--
-- **送信数を後から数える手段が他に無い。**
--   watches.notified_rise_at … 抑制したときにも立つので送信数にならない
--   watches.notified_low_usd … 日付を持たない
--   push_subscriptions.last_sent_at … 端末ごとの最終日しか残らない
--
-- collection_logs と同じく「1回走ったら1行」。KPI はここから読む。
-- ============================================================
CREATE TABLE IF NOT EXISTS notification_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    check_date    TEXT    NOT NULL,
    -- 判定した数と、種類ごとに条件を満たした数。
    watches_checked INTEGER NOT NULL DEFAULT 0,
    low_triggered   INTEGER NOT NULL DEFAULT 0,
    rise_triggered  INTEGER NOT NULL DEFAULT 0,
    -- 実際の送信結果。gone は購読が死んでいて削除したもの。
    sent          INTEGER NOT NULL DEFAULT 0,
    gone          INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_logs_date
    ON notification_logs (check_date);

-- 買い時の判定の記録（2026-10-12 から）。
--
-- **アプリの目的は「いま底値の時期か」を言い当てること**（docs/issues.md 冒頭）。
-- それまで記録していたのは価格の点予測（prediction_log）だけで、**今買う／待つ／
-- 様子見の判定そのものは一度も答え合わせしていなかった。** 出発日を迎えたあとに、
-- 判定した日の価格が底値にどれだけ近かったかを測るために残す。
--
-- ルート単位・絞り込みなし（航空会社も所要時間の上限もかけない）で判定する。
-- 答え合わせの相手は「その日そのルートで最も安かった運賃」なので、条件を揃える。
CREATE TABLE IF NOT EXISTS verdict_log (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_on                TEXT    NOT NULL,   -- 判定した日
    origin                   TEXT    NOT NULL,
    destination              TEXT    NOT NULL,
    departure_date           TEXT    NOT NULL,
    return_date              TEXT    NOT NULL DEFAULT '',
    days_before_departure    INTEGER NOT NULL,
    verdict                  TEXT    NOT NULL,   -- buy_now / wait / watch / insufficient_data
    basis                    TEXT,               -- observed / transferred / simulated ...
    observed_days            INTEGER,
    current_price_usd        REAL,               -- 判定した日の最安（我々の実測）
    predicted_min_usd        REAL,
    predicted_min_upper_usd  REAL,
    predicted_min_date       TEXT,
    rise_date                TEXT,               -- 画面が出している「値上がりが始まる日」
    -- 影の機械学習（2026-10-23）。利用者には見せない。14日以内に5%以上下がる確率
    ml_drop_prob             REAL,
    ml_model                 TEXT,               -- モデルの版@学習日
    created_at               TEXT    NOT NULL,
    UNIQUE (logged_on, origin, destination, departure_date, return_date)
);

CREATE INDEX IF NOT EXISTS idx_verdict_log_route
    ON verdict_log (origin, destination, departure_date, logged_on);
