"""アプリ全体の設定。

既存 flight-price-monitor の config.py はモニター対象を ROUTES 定数として
持っていたが、本アプリは匿名マルチテナントで、収集対象は monitored_routes
テーブルが持つ。したがってここにルートは書かない — 書けば単一ユーザー前提に
逆戻りする。

値は .env から読む（pydantic-settings）。秘密情報はコードに直書きしない。
"""
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- ストレージ ---
    db_path: Path = PROJECT_ROOT / "data" / "flights.db"

    # --- タイムゾーン ---
    timezone: str = "America/Los_Angeles"

    # --- 収集のポライトネス ---
    request_delay_min_seconds: float = 2.0
    request_delay_max_seconds: float = 5.0
    max_retries: int = 3
    retry_backoff_base_seconds: float = 3.0

    # --- 収集の範囲 ---
    max_stops: int = 1

    # 1検索から保存する便の数（安い順）。
    #
    # **2026-09-08 に 5 から 10 へ。** それまでは応答の片方のブロックしか
    # 読んでおらず、手元に来る便が5件前後だったので 5 でほぼ足りていた。
    # 両ブロックを読むようにして SAN→HND では10件返るようになったため、
    # 5 のままだと**せっかく読んだ半分をここで捨てる**ことになる。
    #
    # 検索の回数は変わらないので所要時間には響かない。増えるのは行数だけで、
    # 432ルートで日 2,500 行 → 4,000 行程度、DB は日 1MB 前後（disk 40GB）。
    top_n_itineraries: int = 10

    # 別の出発日の実測から転用するとき、画面に並べる便の数。
    # **保存の数（上）とは別。** こちらは見せる量の話で、増やすと
    # 「1回しか出ていない便」まで一覧に並ぶ。
    top_n_airlines: int = 5

    # 1モニターが持てる収集ルート数の上限。近接空港の展開は組み合わせなので、
    # 近接ペアを増やすとルート数が掛け算で伸びる。現在の設定（SAN/HND/NRT）で
    # は最大4だが、歯止めが無いと設定を足した瞬間に日次リクエストが跳ねる。
    max_routes_per_watch: int = 8

    # LCC 明示検索を何経由まで行うか。0 = 直行便のみ。
    #
    # 1 ルートあたりのリクエスト数に直結する。max_stops=1 のとき
    # 通常検索2回 + LCC 1社あたり (この値 + 1) 回。500ルート上限では
    # 0 なら約1500回、1 なら約2000回で、平均3.5秒間隔だと 30分弱の差になる。
    # 既知の取りこぼしは「LCC が自社運航する直行便の運賃バケット」なので
    # 既定は 0。
    lcc_search_max_stops: int = 0

    # --- 上限（Phase 0 Q4）---
    # 2026-08-14 に 500 → 1000。利用者由来のルート増加が 47本/日 に達し、
    # 残り6日で上限だった。1ルートあたり 11.76 秒なので 1000本でも 196分、
    # 03:00 開始で 06:16 に終わる（利用者が見る時間帯には収まる）。
    #
    # 2026-10-15 に 1000 → 2000。調査用ルートは出発が遠いほど間を空けて引く
    # ようにした（research_cadence）ので、**登録本数と毎朝の取得本数が別物に
    # なった**。SAN/LAX ⇄ NRT/HND の8区間を出発日1日おきで1年分入れると
    # 登録は約1,900本、毎朝の取得は約700本（約3時間）。利用者のルートは
    # 毎日引くので、利用者が急に増えたときは収集時間を見ること。
    max_active_routes: int = 2000

    # 上限のこの割合を超えたら日次ジョブが警告を出す。
    # **上限に達してから気づくと、伸びている最中に受付を止めることになる。**
    route_capacity_warn_fraction: float = 0.8
    max_watches_per_ip_per_day: int = 10
    max_active_watches_per_ip: int = 20

    # 総所要時間（飛行 + 乗継）の既定の上限（分）。モニターごとに変更・解除できる。
    #
    # 24時間。東京〜米西海岸は直行 9〜11時間、通常の1経由で 13〜18時間なので、
    # 乗継が一晩になる便だけが外れる線。実測では NRT-YYC-SAN が $799・総所要
    # 33h10（乗継 20h35）で最安に出ており、これが買い時判定の入力になっていた。
    #
    # 0 以下にすると上限なし扱い。
    max_total_minutes_default: int = 24 * 60

    # 一覧（collections）の放置期限。所属モニターがすべて出発日を過ぎた後、
    # これだけ開かれなければ消す。モニター本体より長くしてあるのは、
    # 一覧が先に消えると「索引を失う」という作った理由そのものが起きるため。
    collection_idle_expiry_days: int = 180

    # 収集しても何も取れないルートを自動で止めるまでの日数。
    # 収集が実際に走った日だけを数える（スケジューラが止まっていた日は数えない）。
    auto_disable_after_failed_days: int = 3

    # 「便が0件」を根拠に止めるまでの日数。**エラーより長く取る。**
    #
    # 0件はエラーより弱い証拠で、上流が不調な日にも出る。2026-09-01 に
    # FUK-HND・HND-OKA・LAX-NRT など実在する路線が17本まとめて0件を返した
    # （上流の IndexError が同時多発した日）。3日のままだと、あと1日続けば
    # 停止していた。**急ぐ理由が無い**ので日数で吸収する。
    #
    # 「一度も取れたことが無い」条件（repository 側）と併用する二重の歯止め。
    auto_disable_after_empty_days: int = 5

    # 航空会社が予約を受け付けはじめる、出発の何日前か。
    #
    # **これより先の出発日は、検索しても便が出てこないのが正常。** その「0件」を
    # 「運航されていない」証拠に数えてはいけない。
    #
    # 2026-10-11 に、これで SAN-NRT の調査用ルート57本を誤って止めた。1年分の
    # 出発日を 10/6 に登録したところ、2027-09-05 以降（登録日から330日超）は
    # まだ予約が始まっておらず、5日連続0件で「運航されていない」と判定された。
    # 一度も取れていないルートなので、「一度でも取れたら止めない」歯止めも効か
    # なかった — 登録したばかりのルートは、取れていないのが当たり前だから。
    #
    # 実測の境目は **328日先**（2026-10-11 時点で便が取れた最も遠い出発日が
    # 2027-09-04）。航空会社によって差があり、一般に330〜355日。
    #
    # **小さめに取るほど安全側。** 小さすぎたときの害は「本当に運航されていない
    # 遠い先の区間を止めるのが少し遅れる」だけで、利用者には響かない。
    booking_horizon_days: int = 330

    # **以前は便が取れていたルートが0件だったら、間を置いて引き直す。**
    #
    # 2026-10-01 ごろから、収集の最中にだけ0件が増えた（9月 1〜5% →
    # 10月 15〜23%）。実在する路線まで空で返り、約8割は翌日に戻る。同じ検索を
    # 収集の時間帯以外に流すと取れた（10/12、本番サーバから確認）。
    # 取得失敗には出ないので、静かにデータが欠けていた。
    #
    # 本ループの後に `empty_retry_delay_seconds` 待ってから1回だけ引き直す。
    # 待つのは、収集の後半ほど空振りが多い（短時間に大量に引くと絞られる）
    # 見立てのため。間を置かずに引き直しても同じ条件にぶつかる。
    #
    # 引き直しで検索が増えて状況が悪くなるようなら、ここで止められる。
    empty_retry_enabled: bool = True
    empty_retry_delay_seconds: int = 600

    # ── 調査用ルートの取得頻度（2026-10-14）─────────────────────────
    #
    # **出発が遠いほど価格はあまり動かない。** 自前の実測で、1日の価格変化
    # （区間最安、平均の絶対値）は 151日以上前 2.8%、91〜150日 4.0%、
    # 61〜90日 4.6%、46〜60日 5.2%、0〜7日 17.8%。遠い日を毎日引いても
    # 得るものが少ない。
    #
    # **調査用のモニターだけが参照するルートに限る。** 利用者のモニターが
    # 参照するルートは毎日引く（グラフが粗くなるのを利用者に見せない）。
    #
    # (出発までの日数の上限, 何日に1回)。上から順に当てる。
    research_cadence_enabled: bool = True
    # ── 影の機械学習（2026-10-23、app/ml/drop_model.py）──────────────
    # 「14日以内に5%以上下がる確率」を判定の記録に残すだけ。利用者には見せない。
    ml_shadow_enabled: bool = True
    # 空なら DB と同じフォルダ。**アプリのフォルダには置かない**（配備で上書きされうる）。
    ml_model_path: Optional[Path] = None
    ml_retrain_days: int = 28        # これより古いモデルは学習し直す
    ml_min_series: int = 30          # 出発済みの便がこれ未満なら学習しない
    research_cadence: tuple[tuple[int, int], ...] = ((60, 1), (150, 3), (100000, 7))

    # **全面障害のときに全部を止めてしまわないための歯止め。**
    # 候補が稼働ルートのこの割合を超えたら、1本も止めずに警告だけ出す。
    # fast_flights は非公式スクレイパで、Google 側の変更で全ルートが同時に
    # 壊れうる。そのとき自動停止が働くと、監視対象を自ら全滅させる。
    auto_disable_max_fraction: float = 0.30

    # 全面障害と見なすのに必要な最小の件数。
    # **割合だけで判断すると、ルートが少ないときに何も止められない**
    # （稼働2本で1本が不良なら 50% になる）。1本の失敗は定義上「全面」では
    # ないので、割合と件数の両方を満たしたときだけ見送る。
    auto_disable_min_widespread: int = 3

    # --- ライフサイクル ---
    watch_idle_expiry_days: int = 60
    watch_post_departure_grace_days: int = 7

    # 出発日まで必ず収集しきるか。
    #
    # **既定を true にしてある。** false（仕様 2.4 の当初の規則）だと、
    # 放置期限のほうが先に来たときに系列が出発日に届かず途中で切れる。
    # 途中で切れた系列は「残り期間の最小値がいくらだったか」を確定できない
    # ので、価格曲線パラメータの推定にも学習にも使えない。
    #
    # 日次コストの天井は変わらない（max_active_routes x 検索数で決まる）。
    # 変わるのはルート枠の回転速度で、放置されたモニターが枠を長く占有する。
    # 枠が逼迫するなら false に戻すか、max_departure_days_ahead を縮める。
    collect_until_departure: bool = True

    # --- KPI（利用状況の日次記録）---
    #
    # ローカルの CSV を正とし、S3 はその写しとして毎日まるごと置き換える。
    # 逆（S3 を正）にすると、アップロードが失敗した日の行が永久に欠ける。
    # 1日1行なので数年ぶん貯めても数十 KB にしかならず、全文送信で足りる。
    kpi_csv_path: Path = PROJECT_ROOT / "data" / "kpi_daily.csv"
    kpi_s3_bucket: str = ""          # 空ならアップロードしない
    kpi_s3_key: str = "kpi/daily.csv"
    kpi_s3_region: str = "us-west-2"

    # 運用ダッシュボードの取得口（/api/ops/dashboard.json）を開く鍵。
    #
    # **空なら 404 を返す。** 鍵を設定しない限りエンドポイントは存在しない
    # のと同じにする。「うっかり公開していた」を起こさないため。
    #
    # 毎朝クラウド側のルーティンがここを叩いてダッシュボードを作り直す。
    # ルーティンは本番の DB にも S3 にも触れないので、渡す口がここだけ要る。
    ops_api_key: str = ""

    # ダッシュボードの書き出し先。**KPI とは別のバケットにする。**
    # KPI のバケットには本番 DB のバックアップが入っており、そこを一部でも
    # 公開読み取りにするわけにはいかない。
    #
    # 置くのは集計と HTML だけで、モニターのトークンや IP ハッシュは
    # 含まない。**接頭辞を知っていることが権限**（取得口の鍵と同じ形）。
    #
    # 空なら書き出さない。
    dashboard_s3_bucket: str = ""
    dashboard_s3_prefix: str = ""
    dashboard_s3_region: str = "us-west-2"

    # --- 通知（課題4）---
    # **公開鍵が空なら通知機能そのものを出さない。** 鍵を入れるまでは
    # 画面に購読ボタンも出さず、購読 API も 503 を返す。設定し忘れた状態で
    # 「通知を受け取る」と見せて、実際には何も来ない、という事故を防ぐ。
    #
    # 鍵は scripts/gen_vapid_keys.py で1回だけ作り、/etc/fpp/env に置く。
    # **作り直すと既存の購読が全部無効になる**（購読は公開鍵に紐づく）。
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    # RFC 8292。プッシュサービスが問題時に参照する連絡先。
    vapid_subject: str = "mailto:admin@suosolutions.com"

    @property
    def push_enabled(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key)

    # --- ログ ---
    log_level: str = "INFO"
    # ログファイルは放っておくと無限に伸びる。常駐プロセスなので必ず回す。
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 3

    # --- スケジューラ ---
    # Web プロセスから日次ジョブを回すかどうか。cron / EventBridge から
    # `python -m app.cli run-daily` を叩く運用にするなら false。
    scheduler_enabled: bool = True
    daily_run_hour: int = 3          # TIMEZONE 基準
    daily_run_minute: int = 0
    # 停止していた間に実行時刻を過ぎていた場合、この猶予内なら追いつく。
    job_misfire_grace_seconds: int = 3600

    # --- Web ---
    # 登録できる出発日の上限（今日から何日先まで）。無制限にすると
    # 「10年先」のような収集し続けるだけのモニターを作られる。
    max_departure_days_ahead: int = 400
    # IP ハッシュのソルト。空なら data/.ip_salt に自動生成する
    # （app/security.py）。本番は Parameter Store から与える。
    ip_hash_salt: str = ""

    # X-Forwarded-For を信用するか。
    #
    # **既定は false。** true にしてよいのは、クライアントが送ってきた
    # X-Forwarded-For をリバースプロキシが必ず上書きする構成のときだけ
    # （Caddy なら `header_up X-Forwarded-For {remote_host}`）。
    # 直接公開したまま true にすると、クライアントが偽の IP を名乗って
    # レート制限（IP あたり10件/日）を素通りできる。
    trust_proxy_headers: bool = False

    # 自動生成 API ドキュメント（/api/docs）。匿名の公開エンドポイントなので
    # 本番では内部構造を広げない。
    docs_enabled: bool = True

    # --- 予測 ---
    # 較正に使う直近の実測点数。1点だと外れ値ひとつで予測全体がズレるので
    # この窓の比率の中央値を取る（2.5 の欠陥1）。
    calibration_window: int = 7

    # 信頼帯の広さ。観測点が少ないほど、また遠い未来ほど広くなる
    # （2.5 の欠陥3）。実測3日分と40日分が同じ確信度に見えてはいけない。
    band_base_uncertainty: float = 0.05   # 観測がばらつかなくても残す最低幅
    band_small_sample_k: float = 1.0      # 観測点数の少なさによる拡大
    band_horizon_k: float = 0.5           # 予測が先に伸びることによる拡大
    band_max_ratio: float = 0.60          # 帯の上限（無意味に広げない）
    # 実測ゼロでモデル推定だけを出すときのばらつき。較正の材料が無いので
    # 曲線の絶対水準をそのまま使うことになり、実測ありの場合より不確か。
    band_no_data_dispersion: float = 0.15
    # 同じルートの「別の出発日」の実測から転用するときに上乗せする不確かさ。
    # 同一出発日の較正より不確かで、まったくの無較正よりは確か、という順序を
    # 帯の広さで表す。
    band_transfer_dispersion: float = 0.08

    # 航空会社が「このルートを飛んでいる」と見なす実測の有効期間（日）。
    # 撤退した路線の社名をいつまでも出さないための足切り。
    airline_evidence_days: int = 180

    # 価格曲線パラメータの JSON。無ければ curve.py の既定値を使う。
    # Phase 3.5 で実測から推定した値を書き出す先。
    curve_params_path: Optional[Path] = None

    # --- 原油価格（初期は無効）---
    # 0 のときは EIA へのアクセスを一切行わない。理由は app/predict/oil.py。
    oil_coefficient: float = 0.0
    oil_lag_months: int = 3
    oil_baseline_usd: float = 80.0
    eia_api_key: str = ""       # .env から読む。コードに書かない

    @property
    def request_delay_range(self) -> tuple[float, float]:
        return (self.request_delay_min_seconds, self.request_delay_max_seconds)


# --- ここから下は .env で上書きしない構造的な設定 ---

# 近接空港マッピング（2.3 の SAN 特例）。
#
# **出発側・到着側の両方に効く。** 到着が SAN なら 出発地 -> LAX を、
# 出発が SAN なら LAX -> 目的地 を候補に足す（app/routing.py）。
# ただし LAX-SAN 間の便自体は収集しない（陸路等で自己接続する前提）。
#
# SAN 専用のハードコードにしないこと。将来 SFO/OAK/SJC のような
# 他の近接ペアを足せるよう、ここを増やすだけで済む形にしてある。
# SAN -> LAX は一方向。到着地が San Diego のとき LAX も見るが、LAX を
# 目的地にしている人に SAN を勧めはしない（行き先が違う）。
# HND <-> NRT は双方向。どちらも東京の空港で、利用者にとって入れ替え可能。
NEARBY_AIRPORTS: dict[str, list[str]] = {
    "SAN": ["LAX"],
    "HND": ["NRT"],
    "NRT": ["HND"],
}

# 航空会社を指定しない通常検索では運賃が拾えない LCC。
#
# 既存 flight-price-monitor の README に記録された未解決バグ:
# ZIPAIR は無指定検索だとほぼ結果に出ないが、airlines フィルタで
# 明示指定すると公式サイトとほぼ一致する運賃が返る。Phase 2 で
# このリストを明示検索の追加クエリに使う。
LCC_CARRIERS: dict[str, str] = {
    "ZIPAIR": "ZG",
}

# prediction_log に予測を残す先の日数（何日先の予測を検証用に保存するか）。
#
# 全予測日を保存すると 500ルート x 便数 x 60日 で日次数十万行になる。
# 検証したいのは「何日先まで当たるか」なので、地平線を絞って
# ルートごとの最安便についてだけ残す（1ルート1日あたり5行）。
PREDICTION_LOG_HORIZON_DAYS: tuple[int, ...] = (1, 3, 7, 14, 30)


@lru_cache
def get_settings() -> Settings:
    return Settings()
