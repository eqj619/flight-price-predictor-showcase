# デプロイ手順（Phase 6.2 以降）

対象: AWS Lightsail（us-west-2 / Micro-1GB / Ubuntu 24.04）
公開 URL: `https://flights.suosolutions.com`

構成の根拠と想定コストは [../docs/aws-deployment-plan.md](../docs/aws-deployment-plan.md)。
**AWS リソースの作成は承認制。** この手順書は承認後に実行する。

```
インターネット ──HTTPS──▶ Caddy(443) ──▶ uvicorn(127.0.0.1:8000)
                          TLS 自動取得        └ APScheduler 毎日 03:00 (PT)
                                              └ SQLite /var/lib/fpp/flights.db
```

| 置き場所 | 中身 |
|---|---|
| `/opt/fpp` | コード（`deploy.sh` が rsync）。`fpp` ユーザ所有 |
| `/var/lib/fpp` | SQLite・ログ・IP ソルト。**systemd が書き込みを許す唯一の場所** |
| `/etc/fpp/env` | 環境変数。`root:fpp` の 0640 |

**SSH(22) は普段閉じてある。** 配備のたびに開けて、終わったら閉じる（[下記](#ssh-の開閉)）。

---

## 手順

### 1. インスタンス作成

```bash
aws lightsail create-instances --region us-west-2 \
  --instance-names flight-price-predictor \
  --availability-zone us-west-2a \
  --blueprint-id ubuntu_24_04 \
  --bundle-id micro_3_0 \
  --tags key=project,value=flight-price-predictor
```

静的 IP を割り当てて固定する（インスタンス再起動で IP が変わると DNS が外れる）。

```bash
aws lightsail allocate-static-ip --region us-west-2 --static-ip-name fpp-ip
aws lightsail attach-static-ip  --region us-west-2 --static-ip-name fpp-ip \
  --instance-name flight-price-predictor
```

### 2. ファイアウォール（443 を開ける）

Lightsail の既定では 22 と 80 しか開いていない。

```bash
aws lightsail open-instance-public-ports --region us-west-2 \
  --instance-name flight-price-predictor \
  --port-info fromPort=443,toPort=443,protocol=TCP
```

80 は Let's Encrypt の HTTP-01 チャレンジに要るので閉じない
（Caddy が HTTPS へリダイレクトする）。

構築が終わったら 22 を閉じる（[SSH の開閉](#ssh-の開閉)）。既定の
`0.0.0.0/0` のままにしない。

### 3. DNS

`suosolutions.com` の DNS 管理画面（Squarespace / Google のネームサーバ）で
**A レコードを1本足すだけ**。

```
flights.suosolutions.com    A    <静的IP>
```

**MX（Google Workspace のメール）と、ルート/`www`（Squarespace のサイト）には
触れない。** サブドメインの追加はこれらと独立している。

反映を確認してから次へ進む。DNS が向く前に Caddy を動かすと証明書の取得に
失敗し、Let's Encrypt のレート制限に当たる。

```bash
dig +short A flights.suosolutions.com    # 静的IP が返ること
```

### 4. 初回構築

```bash
./deploy/ssh-access.sh open
scp -i <key.pem> -r deploy ubuntu@<IP>:~/
ssh -i <key.pem> ubuntu@<IP> 'sudo bash ~/deploy/setup.sh flights.suosolutions.com'
```

`setup.sh` は冪等。`/etc/fpp/env` が既にあれば**上書きしない**
（IP ソルトを作り直さない）。

### 5. コードの配備

SSH は普段閉じてあるので、開けてから配備し、**必ず閉じる**。

```bash
./deploy/ssh-access.sh open           # この端末の IP からの 22 を開ける
./deploy/deploy.sh ubuntu@<IP> <key.pem>
./deploy/ssh-access.sh close          # 閉じる（忘れない）
```

以後の更新もこの3手。`data/` は転送対象から外してあるので、
**サーバ上の価格履歴を手元の開発用 DB で上書きすることはない。**

### 6. 確認

```bash
curl -I https://flights.suosolutions.com/            # 200 + HSTS
curl -sI https://flights.suosolutions.com/w/dummy | grep -i robots   # noindex
curl -s https://flights.suosolutions.com/api/docs -o /dev/null -w '%{http_code}\n'  # 404
```

初回の収集は手動で確かめる（`--dry-run` で件数と所要時間を先に見る）。

```bash
ssh ... "sudo -u fpp bash -c 'set -a; . /etc/fpp/env; set +a;
  exec /opt/fpp/.venv/bin/python -m app.cli collect --dry-run'"
```

**収集を手で流すときは必ずバックグラウンドにする。** ルート数が増えると
10分を超え、SSH クライアント側のタイムアウトで接続が切れる。サーバ側の処理は
生き残るが、結果を見届けられなくなる。

```bash
ssh ... "sudo -u fpp bash -c 'cd /opt/fpp; set -a; . /etc/fpp/env; set +a;
  nohup .venv/bin/python -m app.cli run-daily > /tmp/run.log 2>&1 &'"

# 進捗は collection_logs で見る（終わると1行増える）
ssh ... "sudo -u fpp sqlite3 /var/lib/fpp/flights.db \
  'SELECT check_date,status,routes_checked,records_saved,duration_ms/1000 FROM collection_logs ORDER BY id DESC LIMIT 1;'"
```

そもそも日次スケジューラは常駐プロセス内で動くので、この制約を受けない。
手で流す必要があるのは初回確認と設定変更の直後だけ。

---

## X-Forwarded-For の3点セット

レート制限（IP あたり10件/日）の入力になるので、**次の3つは必ずまとめて動かす。**
どれか1つでも外れると、クライアントが偽の IP を名乗って上限を素通りできる。

| 場所 | 設定 | 役割 |
|---|---|---|
| `Caddyfile` | `header_up X-Forwarded-For {remote_host}` | クライアント由来の値を**捨てて**接続元で上書き |
| systemd unit | `--no-proxy-headers` | uvicorn に勝手に解釈させない。信用箇所をアプリ1か所に集約 |
| `/etc/fpp/env` | `TRUST_PROXY_HEADERS=true` | アプリが XFF を読む。既定は false（安全側） |

Caddy を外して uvicorn を直接公開する構成にするなら、`TRUST_PROXY_HEADERS=false`
に戻すこと。

## 秘密情報を Parameter Store に置かない理由

**Lightsail はサービスロール（EC2 のインスタンスプロファイル相当）に対応していない**
（[AWS ドキュメントで確認](https://docs.aws.amazon.com/lightsail/latest/userguide/security_iam_service-with-iam.html)）。
SSM から読むには IAM ユーザの長期キーをインスタンスに置くことになる。

いま必要な秘密情報は `IP_HASH_SALT`（自前で生成するランダム文字列）と、
未使用の `EIA_API_KEY` だけ。**この2つのために長期キーを常設するほうが危うい**ので、
`setup.sh` がインスタンス上で生成し、`root:fpp` の 0640 で持たせる。

EC2 へ移す場合や、扱う秘密情報が増えた場合は Parameter Store を再検討する。

## SSH の開閉

22 番は**ブラウザ SSH だけに許可**した状態を既定にしてある。全世界から
SSH を叩ける状態は、OS もバージョンも公開されている以上そのままにしない。

```bash
./deploy/ssh-access.sh status   # いまの公開範囲
./deploy/ssh-access.sh open     # この端末のグローバル IP からの 22 を開ける
./deploy/ssh-access.sh close    # ブラウザ SSH のみに戻す
```

| ポート | 既定の公開範囲 |
|---|---|
| 22 | `lightsail-connect`（Lightsail コンソールのブラウザ SSH）のみ |
| 80 | 全世界。**閉じない** — Let's Encrypt の HTTP-01 チャレンジに要る（Caddy が HTTPS へ 308 転送する） |
| 443 | 全世界 |

**閉じても締め出されない。** `cidrListAliases` の `lightsail-connect` が
AWS 側の接続元を表しており、Lightsail コンソールの
「ブラウザで SSH 接続」はそのまま使える。だから作業後は必ず `close` してよい。

**`aws lightsail put-instance-public-ports` を手で叩かないこと。**
このコマンドは渡した集合でルールを**全置換**する。22 だけを指定して実行すると
80 と 443 が消え、サイトが落ちたうえに証明書の更新もできなくなる。
`ssh-access.sh` が常に3ポートまとめて送るのはそのため。

`open` は現在のグローバル IP を `curl checkip.amazonaws.com` で取って
`/32` で許可する。IP が変わったら `open` をもう一度実行すれば追随する。

## 運用

```bash
systemctl status flight-price-predictor
journalctl -u flight-price-predictor -f
sudo tail -f /var/lib/fpp/app.log        # 5MB x 3世代でローテーション
sudo -u fpp sqlite3 /var/lib/fpp/flights.db \
  'SELECT check_date,status,records_saved,errors FROM collection_logs ORDER BY id DESC LIMIT 7;'
```

上記は SSH 越しに実行する。閉じている間は
Lightsail コンソールの「ブラウザで SSH 接続」から同じコマンドを叩ける。

### 健全性の確認は `health` を使う

```bash
sudo -u fpp bash -c 'cd /opt/fpp; set -a; . /etc/fpp/env; set +a;
  exec .venv/bin/python -m app.cli health'
```

**`status` を見てはいけない。** 恒常的に失敗する組み合わせが1つでもあると
毎日 `partial` になり、警告として機能しなくなる（実際
`NRT->SYD stops=1` が `fast_flights` のパーサで再現性のある解析失敗を起こす）。

`health` は失敗を抑制せず、**「今日はじめて出たか」で分けて**見せる。
新しい失敗があれば**終了コード 1** で終わるので、cron から叩いて通知に
繋げられる。

既知リストで握りつぶす方式は採っていない。Google 側の変更でパーサが全面的に
壊れたとき、リストに入っている分は永久に気づけなくなるため。

6.0 の検証では AWS から問題なく取得できたが、リクエスト規模が増えると
Google 側の扱いが変わる可能性はある（プラン1節に退避案）。

### バックアップ

価格履歴はモニター削除後も残す設計なので、失うと痛い。

```bash
aws lightsail enable-add-on --region us-west-2 \
  --resource-name flight-price-predictor \
  --add-on-request 'addOnType=AutoSnapshot,autoSnapshotAddOnRequest={snapshotTimeOfDay=11:00}'
```

`11:00` は UTC 指定。日次収集（03:00 PT = 10:00 UTC）の直後に取る。

---

## Caddyfile を変更したとき

**`deploy.sh` は Caddyfile を配らない。** アプリのコードだけを rsync する。
TLS の入口を毎回の配備で触ると、アプリの小さな変更でサイト全体を落とす
余地が生まれるため、意図的に分けてある。

```bash
./deploy/update-caddy.sh ubuntu@<IP> <key.pem>
```

**リポジトリの `deploy/Caddyfile` は `__DOMAIN__` のままのテンプレート。**
`scp` で直接置くとドメイン定義が消え、サイトが落ちる（2026-08-11 に実際に
落とした）。上のスクリプトは稼働中の設定から実ドメインを読み取って差し込み、
疎通しなければ自動でバックアップへ戻す。

`caddy validate` を root で走らせるとログファイルを root 所有で作ることが
あり、そのままだと caddy が起動できなくなる。スクリプトは reload の前に
`/var/log/caddy` を**再帰的に** chown する。

---

## KPI の S3 出力

日次ジョブが `/var/lib/fpp/kpi_daily.csv` に追記し、`s3://fpp-kpi-<ACCOUNT_ID>/kpi/daily.csv`
へ毎日まるごと置き換える。

**Lightsail にはインスタンスロールが無い**ため、専用 IAM ユーザー `fpp-kpi-writer` の
アクセスキーを `/etc/fpp/env` に置いている（Parameter Store を見送ったのと同じ制約）。

権限は `s3:PutObject` のみ・`kpi/*` 限定。読み取り・一覧・削除・他プレフィックスは
すべて拒否されることを実測で確認してある。バケットはバージョニング有効なので、
キーが漏れて上書きされても復元できる。

キーを入れ替えるとき:

```bash
aws iam create-access-key --user-name fpp-kpi-writer
# /etc/fpp/env の AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY を差し替え
sudo systemctl restart flight-price-predictor
aws iam delete-access-key --user-name fpp-kpi-writer --access-key-id <古いID>
```

**シークレットは標準入力で渡す。** コマンドライン引数に置くとリモートの `ps` に出る。

---

## 費用の通知（AWS Budgets）

| 予算 | 対象 | 限度 | 通知 |
|---|---|---|---|
| `My Monthly Cost Budget ($10)` | アカウント全体 | $10 / 月 | 実績 85% / 実績 100% / 予測 100% |
| `flight-price-predictor (Lightsail)` | Amazon Lightsail のみ | $12 / 月 | 同上 |

通知先は 運用者のメールアドレス（2件）。AWS Budgets のメール宛先は
SNS と違い確認手続きが要らないので、追加した時点で有効になる。

**予算は2つまで無料、3つ目から日額 $0.02（月 $0.62）。** 増やすときは
本プロジェクトの費用の1割近くになることを踏まえて判断する。

プロジェクト用の予算を Lightsail だけに絞っているのは、S3（KPI）が月 $0.01 未満で
誤差にしかならない一方、`Service=S3` で絞ると同一アカウントの無関係なバケット
（amplify・athena の結果置き場）が月 $2.5 ほど混ざり、**本プロジェクトの異常が
その変動に埋もれる**ため。

### Lightsail は無料トライアル中

2026-06〜08 の Lightsail 課金は $0。構成図の $7.00 / 月 はトライアル終了後の額で、
**2026-11 頃から発生する**。それまではアカウント全体の額が本プロジェクトの
実費を表さない。

```bash
aws budgets describe-budgets --account-id <ACCOUNT_ID> \
  --query 'Budgets[].[BudgetName,BudgetLimit.Amount,CalculatedSpend.ActualSpend.Amount]' --output table
```
