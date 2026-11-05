# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

航空券の価格を日次で収集し、出発日までの推移を予測して買い時を提示する Web アプリ。
Python 単一スタック（FastAPI + SQLite + 静的フロントエンド）、匿名マルチテナント。

## コマンド

仮想環境は `.venv/`。**`.venv/bin/python` を明示的に使うこと**（システム Python には依存が入っていない）。

```bash
.venv/bin/pip install -r requirements.txt

.venv/bin/python -m pytest tests/ -q            # 全テスト（ネットワーク不要）
.venv/bin/python -m pytest tests/test_predict.py::test_the_band_is_capped -q   # 単体
.venv/bin/python -m pytest tests/ -q -k band    # 名前で絞る

.venv/bin/python -m uvicorn app.main:app --reload    # http://127.0.0.1:8000/

.venv/bin/python -m app.cli init-db
.venv/bin/python -m app.cli add-route NRT SAN 2026-10-15
.venv/bin/python -m app.cli routes | status
.venv/bin/python -m app.cli collect --dry-run   # 発行クエリ数と所要時間の見積り（通信しない）
.venv/bin/python -m app.cli collect --once      # ★実際に Google Flights を叩く
.venv/bin/python -m app.cli predict [O D DATE] [--log]
.venv/bin/python -m app.cli run-daily           # ★整理→収集→予測記録（通信あり）
.venv/bin/python -m app.cli sync-routes --dry-run   # 近接空港ルール変更後の張り替え
.venv/bin/python -m app.cli dataset                # 学習データとしての蓄積状況
.venv/bin/python -m app.cli health                 # 新しく壊れたものだけを出す（新規あれば exit 1）
.venv/bin/python -m app.cli export --completed-only --out prices.csv
```

★ の付いたコマンドだけが外部ネットワークを使う。テストは一切通信しない
（provider は `get_flights` を差し替え、collector と job は `FakeProvider`）。

`data/` は gitignore 済み（DB・ログ・IP ハッシュのソルト）。

## 仕様と作業の進め方

- `docs/project-prompt.md` が仕様書。**2.4（匿名マルチテナント）と 2.5（予測方式）は「決定済み・変更不可」**
- 各フェーズ完了時に動作を確認し、ユーザーの承認を得てから次へ進む
- **AWS リソースは、構成と想定コストを提示して承認を得るまで作らない**
- **利用者に見える変更を入れたら、更新履歴に1行足すまでが1つの作業。指示を待たない**
  （判断基準は「更新履歴」の節）
- Phase 1〜6 完了。**https://flights.suosolutions.com で稼働中**（Lightsail / us-west-2、
  毎日 03:00 PT に収集）。運用手順は `deploy/README.md`
- 残りは Phase 3.5（曲線パラメータの実測推定）とメール通知

## アーキテクチャ

### コストモデルが構造を決めている

`watches`（ユーザー視点）と `monitored_routes`（収集対象）は `watch_routes` を挟んだ**多対多**。
収集ジョブは `monitored_routes` の `active` 行だけを見て `watches` を辿らない。これにより
**日次 API コストがユーザー数ではなく「異なる (出発, 到着, 出発日) の数」に比例する**。
多対多なのは近接空港ルール（下記）で 1 watch が複数ルートを必要とするため。

### レイヤと依存の向き

```
cli.py / api/routes.py / jobs/daily.py
   ├─ collect/collector.py ──> collect/provider.py (ABC) <── collect/fast_flights_provider.py
   ├─ predict/engine.py ──> predict/simulator.py ──> predict/curve.py
   └─ db/repository.py ──> db/connection.py ──> db/schema.sql
```

- **SQL は `db/repository.py` にしか書かない。** 参照元の PI-27-flightTrend が SQL を
  ルートハンドラに直書きしてストレージ差し替え不能になっていた
- **`fast_flights` を import してよいのは `fast_flights_provider.py` だけ。**
  collector と cli は関数内で遅延 import する
- **`config.py` にルート定数を置かない。** 置いた瞬間に単一ユーザー前提へ逆戻りする

### 予測パイプライン

実測（`source='real'` のみ）→ 較正 → 曲線を出発日まで外挿 → 信頼帯 → 3値判定。

`build_trend` は根拠の確かな順に3段階へ落ちる。`basis` フィールドがどれかを示し、
**UI は必ずこれを見て表示を変える**（混ぜると PI-27-flightTrend と同じ
「シミュレーション値を実測のように見せるダッシュボード」になる）。

| basis | 条件 | 出せるもの |
|---|---|---|
| `observed` | この出発日の実測がある | 3値判定 + 社名 |
| `transferred` | 同ルートの**別の出発日**に実測がある | 社名（実際に観測された便のみ、便ごとに較正）。3値判定は出さない |
| `simulated` | このルートの実測が皆無 | 曲線のみ。**社名は出さない** |

`transferred` で社名を出せるのは「このルートで実際に返ってきた便」に限る。
`curve.py` の係数表から社名を並べてはいけない — その社が飛んでいない路線の
運賃を出すことになる（ZIPAIR は NRT-SAN の通し運賃を売っていない）。
価格も係数表ではなく**その便自身の過去実測で較正**する。係数表は実測の表記
（"United", "ZIPAIR Tokyo"）と一致しないので、頼ると全社同額になる。

`transferred` が成り立つのは、出発日そのものの効果（季節・曜日・出発までの
日数）を曲線側が織り込んでいるため。残るスケールは便の水準なので持ち越せる。
帯の広さは `observed` < `transferred` < `simulated` の順（確かさの順序を
そのまま幅で表す）。

`price_shape()` の係数のうち **`check_date` とともに動くのは `proximity_factor` だけ**。
基本運賃・航空会社・経由・季節・曜日はすべて出発日だけで決まるので、較正のスケール項
（実測 / 曲線の比の中央値）に丸ごと吸収される。つまり:

- 航空会社名が実測（"United", "ZIPAIR Tokyo"）と `curve.py` の表で一致しなくても実害は小さい
- Phase 3.5 で実測から推定すべき主対象は `proximity_factor` の5パラメータ

3値判定は信頼帯の重なりだけで決める（`engine._decide`）。固定の確信度パーセントは使わず、
帯の広さと「実測◯日分」ラベルをそのまま根拠として出す。

## 課題

既知の課題は `docs/issues.md`。**新しい課題は末尾に追加し、番号を振り直さない**
（過去のやりとりで番号を参照するため）。報告と実測を書き分ける。

## 壊しやすい不変条件

移植元プロジェクトで実際に起きた不具合と、その構造的な対策。**変更前にコメントを読むこと。**

| 場所 | 守るべきこと |
|---|---|
| `snapshots.price_usd` の意味 | **「その便の運賃」ではなく「その日その便を買える最安値」。** 取得元は売り手ごとの内訳を返さず、1便につき価格は1つ（実測で確認）。売り手が入れ替われば運賃が動かなくても値が動く。学習の目的変数を決めるときはこの定義で扱う（`docs/issues.md` 課題1） |
| `repository.history_for` | 既定の `sources=("real",)` を広げない。simulated 行が較正と推奨に混入する |
| `collector.collect_route` | `source="real"` 固定。シミュレーション値をここから入れない |
| `fast_flights_provider._flight_details` | 取得できた項目は全部持ち帰る。**遡って取得できない**ので、いま使わなくても捨てない。項目単位で欠けることが実際にある（ANA の出発時刻が None で返った）ので `getattr` で防御し、収集は止めない |
| `schema.sql` の `via_airport` | NULL ではなく `''`。SQLite の UNIQUE は `NULL != NULL` なので直行便の重複排除が黙って壊れる |
| `collector._cheapest_distinct` | 保存前に (経由回数, 経由地, 航空会社) ごと最安1件へ畳む。`ON CONFLICT DO UPDATE` は後勝ちなので、畳まないと高い方が残る |
| `fast_flights_provider._search` | 「該当便なし」は `FlightsNotFound` と bare `TypeError` の**2通り**で来る。どちらもエラーではないので即 `[]`（リトライしない）。一般の失敗として扱うと、在庫の出ていない遠い出発日で `fetch_errors` が埋まり本当の障害が隠れる |
| `fast_flights_provider.fetch` | `len(fg.flights) - 1 == stops` で検証する。`max_stops` は「以下」なので直行便が 1-stop として二重計上される |
| `predict/simulator.py` | 較正と予測は `price_shape()`（ゆらぎ・丸めなし）。`simulate_price()` は simulated スナップショット生成用 |
| `app/clock.py` | 「今日」は `clock.today()`。`date.today()` は OS のタイムゾーンに従ってしまう |
| `jobs/daily.py` | 整理 → 収集 の順。逆だと今日失効するモニターのために取りに行く。例外を投げない |
| `repository.expires_at_for` | 既定（`collect_until_departure=true`）では放置期限で系列を切らない。切ると「残り期間の最小値」が確定せず、学習にも曲線推定にも使えない列になる |
| `deploy/Caddyfile` | `__DOMAIN__` のままのテンプレート。**scp で直接置かない**（ドメイン定義が消えてサイトが落ちる）。反映は `deploy/update-caddy.sh` を使う |
| `expand_routes` の既定 | **`nearby_airports=False`（2026-08-14 変更）。** 1モニターが最大4ルートに展開され、枠の消費が利用者数の 2.4倍になっていた。**既存の watches は自分の設定を明示的に渡すので影響しない** — 既定を変えたからといって、監視中の区間を黙って止めてはいけない |
| `jobs.check_route_capacity` | 止めない。判断は人がする。上限に達してから気づくと、利用が伸びている最中に受付を止めることになるので8割で警告する |
| `jobs.disable_failing_routes` | **全面障害の歯止めを外さない。** 候補が稼働ルートの一定割合を超え、かつ一定件数以上なら1本も止めない。fast_flights は非公式スクレイパで全ルートが同時に壊れうる。割合だけで判断すると、稼働2本で1本が不良でも 50% になり小規模では永久に止められない |
| `repository.routes_failing_consistently` | 数えるのは**収集が実際に走った日**だけ。スケジューラが止まっていた日を失敗に数えると、こちらの都合でルートを止める。実測が1件でもあるルートは対象外（stops=1 が落ちても stops=0 が取れることがある） |
| `repository._collection_expiry` | 一覧の期限は**所属モニターの最長期限より後**にする。先に切れると索引を失い、この仕組みを作った理由そのものが起きる |
| `app/security.py` の `_TOKEN_PATH` | 接頭辞を `/api/watches/` のように前方まで含めない。`/api/collections/{一覧}/watches/{モニター}` は1つのパスにトークンが2つ載るので、後ろ側が伏字にならなくなる |
| `static/js/index.js` の `bootstrap` | 一覧トークンは **URL > localStorage** の順で決める。逆にすると、別端末でブックマークを開いても手元の一覧が出る |
| `app/airports.py` と `static/js/airports.js` | 補完と検証は**同じ `static/airports.json`** を見る。別々のリストにすると、画面で選べるのにサーバが拒否する食い違いが起きる |
| `scripts/build_airports.py` の出力列 | 追加は末尾のみ。JS は添字で読むので、途中に挿入すると静かにずれる（`test_served_rows_match_what_the_frontend_reads` で固定） |
| `airports._data` | マスタが読めないときは空で返さず例外にする。空だと全登録が「未知の空港」で拒否され、原因の分からない全面停止になる |
| `fast_flights_provider._layover_minutes` | 総所要に到着時刻 − 出発時刻を使わない。現地時刻なので日付変更線で逆転する（NRT 18:35 発 → YYC 12:45 着）。乗継は同一空港内なので引き算が成立する |
| `engine._within_duration` | `total_minutes` が NULL の行は**残す**。「不明」を「長すぎる」と同じに扱うと、列を足す前の実測が較正の入力からまるごと消える |
| `api/schemas.WatchUpdate.max_total_minutes` | 0 = 上限なし、None = 変更しない。「上限なし」を None にすると「変更しない」と区別できない |
| `app/kpi.py` の `COLUMNS` | 列は**末尾にしか足さない**。途中に挿入すると過去に書いた行と列がずれ、貯めた履歴がまとめて使えなくなる。意味を失った列も消さず 0 を書き続ける |
| `kpi.upload_to_s3` | 例外を投げない。S3 が落ちていることが価格収集の失敗になってはいけない。ローカル CSV が正で、S3 は写し |
| `tests/conftest.py` | `KPI_CSV_PATH` を tmp に差し替える。DB だけ隔離しても、`run_daily` を呼ぶテストが本物の `data/kpi_daily.csv` に行を足す |
| `jobs/scheduler.py` | ガードは `finally` で必ず解放する。解放し損ねると以後すべての実行が黙って飛ばされ続ける |
| `tests/conftest.py` | `get_settings` は `lru_cache` 済み。環境変数を差し替えたら `cache_clear()` |
| `api/schemas.py::WatchUpdate` | 各項目は `Optional` で「省略 = 変更しない」。既定値を持たせると、片方だけ変えたときにもう片方が既定に戻る |
| `db/connection.py::_ADDED_COLUMNS` | 既存 DB へのカラム追加はここに書く。`schema.sql` だけ直しても稼働中の DB には入らない |

### 近接空港ルール（2.3）

`config.NEARBY_AIRPORTS`（`SAN->LAX` 片方向、`HND<->NRT` 双方向）を引き、
**出発側・到着側の組み合わせで**展開する。`SAN->HND` なら SAN/LAX × HND/NRT の4通り。

- **近接空港どうしを結ぶ区間は収集しない**（LAX-SAN、HND-NRT）。陸路で移動する前提
- 掛け算で伸びるので `max_routes_per_watch` で頭打ちにする
- watch ごとに `nearby_airports` 列で on/off できる
- `expand_routes` は**登録時にしか走らない**。ルールを変えたら
  `app/watches.py::sync_watch_routes`（CLI の `sync-routes`）で張り替える

一時期「片側ずつ」に絞っていたが、`HND<->NRT` を足した時点で誤りになった。
片側ずつだと `SAN->HND` から `LAX->NRT` が漏れるが、実測で最安だった ZIPAIR は
まさに NRT-LAX 路線を飛んでいる。実測での差:

| ルート | 最安 |
|---|---|
| HND → SAN (2026-10-29) | $1,052 |
| HND → LAX (2026-10-29) | **$617** |

### LCC 明示検索

航空会社無指定の検索では ZIPAIR の運賃バケットが拾えない（実測: 無指定 ANA $2,172 に対し
`airlines=["ZG"]` 指定で ZIPAIR $494）。`config.LCC_CARRIERS` を使った追加クエリを発行し、
**検索ごとに独立して top_n を取る**。全検索をまとめてから切ると、取りこぼしを拾うための
検索結果が通常検索の安値に押し出されて消える。

### トークンの扱い（capability）

`/w/{token}` は URL を知っていれば誰でも開ける。認証がない代わりに
**トークンがどこにも平文で残らないこと**が前提。塞ぐ経路は3つ:

1. アクセスログ / エラーログ — `security.install_log_redaction()` が `/w/<token>` に伏字化
2. 外部送信先 — **Chart.js は CDN ではなく `static/vendor/`**。CDN から読むと
   Referer ヘッダに `/w/{token}` が載る。併せて `Referrer-Policy: no-referrer`
3. 検索エンジン — `X-Robots-Tag` と `<meta>` の両方で noindex

`/w/{token}` は存在しないトークンでも 200 で同じ HTML を返す。ここで 404 を出し分けると
HTML の有無だけでトークンの実在を総当たり判定できる。IP は生で保存せずソルト付き sha256。

### 更新履歴（エンドユーザー向け）

`static/js/changelog.js`。**利用者に見える変更を入れたら、指示が無くても
必ずここに1行足す。** 後追いの別作業にせず、その変更と同じコミット・同じ配備で出す。

**判断は1つの問いで決まる — 「利用者が見るもの、できることが変わったか」。**

| 書く | 書かない |
|---|---|
| 機能の追加・変更・削除 | 内部の不具合修正・リファクタリング |
| 画面に出る情報や既定の挙動が変わった | 運用・配備・監視の変更（`health`、収集手順など） |
| 対応範囲（路線・空港・言語）が広がった | 運用者しか使わない CLI コマンド |
| | 既に書いた項目の手直し（表示崩れの修正、タブの位置調整） |

- **1機能 = 1行。**コミット単位で分けない。同じ機能の修正や位置調整を別項目に
  すると、利用者には何が変わったのか読みにくくなる
- 日英を両方書く。片方だけだと言語を切り替えたときに項目が欠ける
- 新しいものを配列の先頭へ。日付は**利用者に届く日**（PT）
- **配備するまで完了ではない。**書いただけで配備していない項目は、
  利用者には存在しない。配信されているファイルで確認する

### フロントエンド

`static/` は素の JS（ビルド工程なし）。i18n は PI-27-flightTrend の
辞書 + `data-i18n` + localStorage パターンを流用。
**localStorage は正ではない** — サーバのトークンが正で、localStorage は
「このブラウザから作ったものの索引」。開くたびにサーバへ問い合わせ、失効していれば索引から落とす。
予測は必ず信頼帯付きで描画し、判定には「実測◯日分」を併記する（帯を UI で潰さない）。
