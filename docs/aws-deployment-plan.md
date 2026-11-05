# Phase 6 — AWS デプロイ【完了】

**稼働中: https://flights.suosolutions.com**（2026-08-09 開通）

作成日: 2026-08-09 / 対象: `flight-price-predictor` Phase 1〜5 完了時点

| 段階 | 状態 |
|---|---|
| 6.0 スパイク（AWS から取得できるか） | 合格・インスタンス削除済 |
| 6.1 デプロイ資材 | `deploy/` 一式 |
| 6.2 インスタンス・静的IP・443 | `flight-price-predictor` / `<STATIC_IP>` |
| 6.3 DNS + TLS | Let's Encrypt（2026-11-07 まで・自動更新） |
| 6.4 自動スナップショット | 毎日 11:00 UTC |
| 6.5 本番投入 | モニター2件・初回収集 success（18件・エラー0） |

実際の手順は [../deploy/README.md](../deploy/README.md)。

---

## 1. 検証結果 — AWS から Google Flights を取得できるか【2026-08-09 実施・合格】

**結論: 取得できる。自宅から取った値と完全に一致した。**

us-west-2 に Lightsail（Micro-1GB / Ubuntu 24.04 / IP 44.246.136.114）を1台立て、
同じ日・同じ4ルートで `collect --once` を実行し、同日の自宅取得分と突き合わせた。

| | AWS (us-west-2) | 自宅 |
|---|---|---|
| 結果 | success | success |
| 保存レコード | 23 | 21 |
| エラー | 0 | 0 |
| 所要 | 47.3 秒 | 52.5 秒 |
| 最安 | **ZIPAIR Tokyo $494** | **ZIPAIR Tokyo $494** |

- **両方に出た便17件は、17件とも価格が1ドル単位で一致**（ANA $2,220 / American $907 /
  Korean Air $628 / ZIPAIR $494 …）
- CAPTCHA・ブロック・空応答は発生しなかった
- **LCC 明示検索（`airlines=["ZG"]`）も機能した。** ZIPAIR $494 を AWS 側でも取得できて
  いる。ここが取れないと本アプリの価値の中心が失われるので、重要な確認点だった
- 一致しなかった 6件/4件は、いずれも各検索の `top_n_airlines`(5) の境界に近い便で、
  実行ごとに入れ替わる範囲。取得元の地域差ではない

検証後、インスタンスは**削除済み**（残存リソース: なし）。実費は 1 セント未満。

### 残る注意点

これは1回の実行結果でしかない。本番では日次 1,500 リクエスト規模になりうるので、
**運用開始後に `collection_logs` の status と `fetch_errors` の件数を継続して見る**。
ブロックが始まった場合の退避案は下記のとおり。

### 通らなくなった場合の選択肢

| 案 | 内容 | コスト影響 |
|---|---|---|
| ハイブリッド | 収集は自宅の Mac（launchd）、配信のみ AWS。`SCHEDULER_ENABLED=false` で配信側のスケジューラを止め、収集結果を同期する仕組みを別途作る | 同額。実装が追加 |
| プロキシ経由 | `fast_flights` は `get_flights(..., proxy=...)` を受け付ける。住宅用プロキシサービスを挟む | +$10〜50/月 |
| データソース差し替え | Amadeus Self-Service API などの公式 API へ。`PriceProvider` 抽象を入れたのはこのため | 無料枠あり。実装 1〜2 日 |

---

## 2. このアプリが構成に課す制約

コードから導かれる、選択肢を絞る条件。

| 制約 | 根拠 | 帰結 |
|---|---|---|
| **SQLite ファイル** | `app/db/` | 単一インスタンス + 永続ディスク。水平スケールしない |
| **日次ジョブが長い** | 実測4ルートで53秒。上限500ルートなら 1500リクエスト × 平均3.5秒 ≈ **87分** | Lambda の15分上限を超える。常駐プロセスか分割実行が要る |
| **常駐スケジューラ** | `app/jobs/scheduler.py`（APScheduler） | 常時起動のプロセスが前提。cron 化するなら `run-daily` に切り替え可 |
| **トークンが URL に載る** | `/w/{token}` が capability | **HTTPS 必須。** 平文 HTTP だと経路上でトークンが漏れ、誰でも閲覧・削除できる |
| **秘密情報** | `IP_HASH_SALT`, `EIA_API_KEY` | Parameter Store（standard は無料）。Secrets Manager は $0.40/件・月なのでこの規模では過剰 |
| **価格履歴は資産** | モニター削除後も残す設計 | バックアップが要る |

データ量は問題にならない。最大規模でも年 300MB 程度（1日あたり実測 5,500行 +
予測 2,500行）。

---

## 3. 構成案

### 案A: Lightsail 1台 ← **推奨**

```
インターネット
    │ HTTPS (443)
    ▼
Lightsail インスタンス (Micro-1GB, us-west-2)
    ├─ Caddy          … TLS 自動取得(Let's Encrypt) + リバースプロキシ
    ├─ uvicorn        … 127.0.0.1:8000 で待受 (systemd で常駐)
    │    └─ APScheduler … 毎日 03:00 (America/Los_Angeles)
    └─ /var/lib/fpp/flights.db  … SQLite
         └─ 自動スナップショット（日次）
Parameter Store … IP_HASH_SALT / EIA_API_KEY
Route 53        … ドメイン（既存ドメインがあれば不要）
```

**なぜ Nano-0.5GB($5) ではなく Micro-1GB($7) か** — Python + FastAPI +
`fast_flights` の HTML パースで、収集中に 0.5GB はスワップしかねない。差額 $2
で余裕を取る。ディスクは 40GB あり、年 300MB の増加に対して十分。

**なぜ Lightsail か** — 固定料金にインスタンス・SSD・静的IP・2TB のデータ転送が
含まれ、この規模では EC2 を個別に組むより安く単純になる。参照元
`PI-27-flightTrend` も Lightsail を想定していた。

### 案B: EC2 t4g.small + EBS + Elastic IP

同じ構成を素の EC2 で組む。Lightsail より項目が増え（EBS・Elastic IP・データ
転送を個別課金）、この規模では割高になりやすい。将来 VPC 内の RDS や他サービス
と統合する必要が出たときの移行先。**いま選ぶ理由はない。**

### 案C: Lambda + API Gateway + DynamoDB/EFS

- SQLite を捨てる必要がある。`repository.py` に SQL を閉じ込めてあるので不可能では
  ないが、実装は数日規模
- 87分のジョブは Step Functions で分割が要る
- アイドルコストはほぼ 0

**現時点では複雑さが見合わない。** 利用が増えて常時起動が無駄になったら再検討。

---

## 4. 想定コスト（月額 / us-west-2）

| 項目 | 月額 | 備考 |
|---|---|---|
| Lightsail Micro-1GB (IPv4付) | **$7.00** | 2 vCPU / 1GB / 40GB SSD / 2TB 転送（AWS 公式価格表で確認済み） |
| 自動スナップショット | 約 $0.20 | 実使用量ベース。要確認 |
| Route 53 ホストゾーン | $0.50 | **既存ドメインを使うなら $0** |
| ドメイン登録（.com 年額を按分） | 約 $1.10 | 既存ドメインがあれば $0 |
| Parameter Store (standard) | $0 | 公式に「no additional charge」 |
| データ転送 | $0 | 2TB 込み。この用途では到底使い切らない |
| **合計** | **約 $8〜9/月** | 既存ドメイン利用なら **約 $7.2/月** |

初月は Lightsail の $7 プランが 750時間まで無料（AWS 公式）。

**IPv6-only プラン（$5/月）は選ばない** — IPv4 しか持たない環境から見えなく
なるため。

---

## 5. デプロイ前に直すべきコードの問題

動作はしているが、公開前に手を入れるべき点。**いずれも小さい。**

| # | 問題 | 対応 | 状態 |
|---|---|---|---|
| 1 | **`X-Forwarded-For` を無条件に信用していた**。ヘッダはクライアントが自由に付けられるので、**レート制限（IP あたり10件/日）を素通りできた** | 既定で信用しない（`TRUST_PROXY_HEADERS=false`）。本番は Caddy で `header_up X-Forwarded-For {remote_host}` と上書きしたうえで true にする | **済** |
| 2 | `/api/docs`（Swagger UI）が公開される | `DOCS_ENABLED=false` で `/api/docs` と `/api/openapi.json` ごと消える | **済** |
| 3 | **ログが無限に伸びる**。`FileHandler` でローテーションしない | `RotatingFileHandler`（5MB × 3世代） | **済** |
| 4 | `data/.ip_salt` がインスタンス再作成で変わる | `setup.sh` がインスタンス上で生成し `/etc/fpp/env`（`root:fpp` 0640）に置く。**Parameter Store は使わない**（下記） | **済** |
| 5 | 参照元 `deploy/lightsail-setup.sh` は Node.js 用で、`git clone` 行がコメントアウトのまま未完成。**そのままは使えない** | `deploy/` 一式を Python 用に新規作成 | **済** |
| 6 | HTTP → HTTPS リダイレクトと HSTS | Caddy が既定でリダイレクト。HSTS は `Caddyfile` で明示 | **済** |

1〜3 は AWS の検証結果に左右されない（ローカル運用にも効く）ため先に対応した。
4〜6 は 6.1 で対応済み。

### 訂正: 秘密情報を Parameter Store に置かない

当初は `IP_HASH_SALT` を Parameter Store から与える想定だったが、**Lightsail は
サービスロール（EC2 のインスタンスプロファイル相当）に対応していない**ことが
分かった（[AWS ドキュメント](https://docs.aws.amazon.com/lightsail/latest/userguide/security_iam_service-with-iam.html)に
"Lightsail does not support service roles" と明記）。

SSM から読むには IAM ユーザの**長期キーをインスタンスに常設する**ことになる。
いま必要な秘密情報は自前で生成するランダム文字列1つと、未使用の
`EIA_API_KEY` だけなので、**そのために長期キーを置くほうが危うい。**
`setup.sh` がインスタンス上で生成し、`root:fpp` の 0640 で持たせる方式に変更した。

EC2 へ移す場合や、扱う秘密情報が増えた場合は Parameter Store を再検討する。

### X-Forwarded-For は3点セット

レート制限の入力になるので、次の3つは必ずまとめて動かす。1つでも外れると
クライアントが偽の IP を名乗って上限を素通りできる。

| 場所 | 設定 |
|---|---|
| `deploy/Caddyfile` | `header_up X-Forwarded-For {remote_host}`（クライアント由来の値を捨てる） |
| `deploy/systemd/*.service` | `--no-proxy-headers`（uvicorn に解釈させない） |
| `/etc/fpp/env` | `TRUST_PROXY_HEADERS=true`（アプリで読む。既定は false） |

---

## 6. 決めていただきたいこと

| # | 項目 | 決定 |
|---|---|---|
| 1 | **公開 URL** | **`https://flights.suosolutions.com`（2026-08-09 決定）** |
| 2 | リージョン | us-west-2（オレゴン）を推奨。`TIMEZONE` が America/Los_Angeles で、Google Flights の結果は取得元の地域で変わりうるため、現在の収集環境と揃う |
| 3 | 公開範囲 | インターネット全体（匿名マルチテナントは URL 共有が前提） |
| 4 | 収集の実行場所 | **1節の検証結果で決まる** |

### ドメインの状態（2026-08-09 時点で確認済み）

| 項目 | 現状 |
|---|---|
| レジストラ | Squarespace Domains II LLC |
| ネームサーバ | `ns-cloud-e1〜e4.googledomains.com` |
| MX | Google Workspace（`aspmx.l.google.com` 他） |
| ルート `suosolutions.com` | `198.185.159.144`（Squarespace のサイト） |
| `www` | `ext-sq.squarespace.com` |
| **`flights`** | **未使用（空き）** |
| CAA | **なし** → Let's Encrypt の発行に制限なし |

**追加するのはサブドメインの A レコード1本だけ。**

```
flights.suosolutions.com    A    <Lightsail の静的IP>
```

**MX（Workspace のメール）とルート/`www`（Squarespace のサイト）には触れない。**
サブドメインの追加はこれらと独立しているので、既存のメールもサイトも影響を受けない。

ドメインを保有しているため **Route 53 とドメイン登録費は不要**。月額は約 $7.2 に下がる。

---

## 7. 承認後の進め方

各段階の完了時に結果を報告し、承認を得てから次へ進む。

| 段階 | 内容 | 作成するリソース | コスト |
|---|---|---|---|
| ~~**6.0**~~ | ~~**スパイク**: AWS の IP から Google Flights を取得できるか検証~~ | ~~Lightsail 1台（一時）~~ | **完了・合格（2026-08-09、削除済み、1セント未満）** |
| ~~6.1~~ | ~~デプロイ前のコード修正（5節の6項目）、systemd unit と Caddyfile、デプロイスクリプト~~ | ~~なし（リポジトリのみ）~~ | **完了（`deploy/` 一式、$0）** |
| 6.2 | インスタンス構築、アプリ配置、Parameter Store に秘密情報 | Lightsail 1台、SSM パラメータ2件 | $7/月 |
| 6.3 | ドメイン設定と TLS | Route 53（必要なら） | +$0.5/月 |
| 6.4 | 自動スナップショット、CloudWatch 相当の死活監視、ログローテーション確認 | スナップショット設定 | +$0.2/月 |
| 6.5 | 本番投入。数日運転して収集が安定するか確認 | — | — |

6.0 は合格したため、6.1 以降に進んでよい。

### 旧インスタンスの停止【完了】

参照元 PI-27-flightTrend のインスタンス `flighttrend`（nano_3_0 / $5.00 月 /
2026-07-17 稼働）を **2026-08-09 に削除**した。本プロジェクトの稼働確認後に
停止する方針どおり。

削除前に中身を確認したところ、`price_records` は **0 行**で、DB の
12,400 行はすべて `simulated_prices`（シミュレータ出力）だった。実測データは
存在しなかったため、失われたものはない。念のため DB は
`PI-27-flightTrend/data/flights-from-lightsail-20260809.db` に退避してある。

**停止ではなく削除**したのは、Lightsail が停止中も課金を続けるため。
アタッチされていない静的 IP も課金対象なので、`StaticIp-1`
（44.229.124.72）も併せて解放した。

削除後の us-west-2 の残存リソースはインスタンス1台と静的 IP 1件のみ
（いずれも本プロジェクト）。

## 8. 今回やらないこと

- **メール通知（SES）** — スキーマに `notify_email` 列があるだけ。SES はサンドボックス
  解除の申請が要るので、必要になってから
- **マルチAZ・冗長化** — SQLite 単一インスタンスの設計上できない。必要になったら
  Postgres 移行が先（`repository.py` に SQL を閉じ込めてあるので移行余地はある）
- **CDN (CloudFront)** — 静的ファイルは Chart.js 206KB と自前の JS/CSS のみ。
  この規模では Caddy の直配信で足りる
- **IaC (Terraform/CDK)** — 1インスタンス構成でリソースが数個。手順書 + シェル
  スクリプトのほうが総コストが低い。構成が増えたら再検討
