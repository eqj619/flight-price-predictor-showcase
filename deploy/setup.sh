#!/usr/bin/env bash
#
# Lightsail インスタンスの初回構築。**何度実行しても同じ結果になる。**
#
#   ssh ubuntu@<IP>
#   sudo bash setup.sh flights.suosolutions.com
#
# 参照元 PI-27-flightTrend の deploy/lightsail-setup.sh は Node.js 用で、
# git clone 行がコメントアウトのまま未完成だった。こちらは Python 用に
# 書き直したもので、コードの配置は deploy.sh（手元から rsync）が行う。
set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  echo "usage: sudo bash setup.sh <domain>" >&2
  exit 1
fi

APP_USER=fpp
APP_DIR=/opt/fpp
DATA_DIR=/var/lib/fpp
ENV_DIR=/etc/fpp
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "▶ 1/7 パッケージ"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip sqlite3 rsync ca-certificates curl

echo "▶ 2/7 Caddy"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
fi

echo "▶ 3/7 ユーザとディレクトリ"
# ログインシェルを持たないサービス専用ユーザ。
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR" "$DATA_DIR" "$ENV_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"
chmod 750 "$DATA_DIR"
# ディレクトリのグループも fpp にする。ファイルを root:fpp 0640 にしても、
# 親ディレクトリを辿れなければ読めない（0750 root:root だと fpp は入れない）。
chown root:"$APP_USER" "$ENV_DIR"
chmod 750 "$ENV_DIR"

echo "▶ 4/7 環境変数"
# 秘密情報は Parameter Store ではなくここに置く。
#
# Lightsail は EC2 と違いインスタンスロール（サービスロール）に対応して
# いないため、SSM から読むには IAM ユーザの長期キーをインスタンスに置く
# ことになる。「1つのランダム文字列を読むために長期キーを置く」ほうが
# root 所有 0600 のファイルより危ういので、ここで生成して持たせる。
if [ ! -f "$ENV_DIR/env" ]; then
  cat > "$ENV_DIR/env" <<EOF
DB_PATH=$DATA_DIR/flights.db
TIMEZONE=America/Los_Angeles

# 本番では内部構造を広げない
DOCS_ENABLED=false

# Caddy が X-Forwarded-For を {remote_host} で上書きする前提。
# **Caddyfile の header_up と systemd の --no-proxy-headers と3点セット。**
# どれか1つでも外すとレート制限を偽装で抜けられる。
TRUST_PROXY_HEADERS=true

# 日次収集（TIMEZONE 基準）
SCHEDULER_ENABLED=true
DAILY_RUN_HOUR=3
DAILY_RUN_MINUTE=0

# IP ハッシュのソルト。生の IP は保存しない。
# インスタンスを作り直すとレート制限のカウントがリセットされるだけで、
# 実害はない。
IP_HASH_SALT=$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-40)

# 原油価格は無効のまま（app/predict/oil.py に理由）
OIL_COEFFICIENT=0.0
EIA_API_KEY=
EOF
  echo "  $ENV_DIR/env を生成（ソルトは新規発行）"
else
  echo "  $ENV_DIR/env は既存のものを維持（ソルトを作り直さない）"
fi
chown root:"$APP_USER" "$ENV_DIR/env"
chmod 640 "$ENV_DIR/env"

echo "▶ 5/7 systemd"
install -m 644 "$SCRIPT_DIR/systemd/flight-price-predictor.service" \
  /etc/systemd/system/flight-price-predictor.service
systemctl daemon-reload
systemctl enable flight-price-predictor >/dev/null

echo "▶ 6/7 Caddy 設定（$DOMAIN）"
mkdir -p /var/log/caddy
sed "s/__DOMAIN__/$DOMAIN/g" "$SCRIPT_DIR/Caddyfile" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile >/dev/null

# **chown は validate の後に、再帰で。**
# `caddy validate` は root で走り、設定を読み込む過程でログファイルを
# 実際に作ってしまう（/var/log/caddy/access.log が root:root 0600 で残る）。
# ディレクトリだけ chown しても、その中の root 所有ファイルは caddy ユーザが
# 開けず、サービスが "permission denied" で起動に失敗する。
chown -R caddy:caddy /var/log/caddy

systemctl enable caddy >/dev/null

# DNS がこのインスタンスを向くまで Caddy は起動しない。
#
# 向いていない状態で起動すると、Caddy は証明書の取得を試みて失敗し続ける。
# Let's Encrypt は「同一ホスト名の検証失敗」に回数制限があるので、
# 空振りを積み重ねると、本当に DNS を向けた後もしばらく取得できなくなる。
MY_IP="$(curl -s --max-time 5 https://checkip.amazonaws.com || true)"
DNS_IP="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || true)"

if [ -n "$MY_IP" ] && [ "$DNS_IP" = "$MY_IP" ]; then
  systemctl restart caddy
  echo "  DNS が一致（$DNS_IP）。Caddy を起動して証明書を取得する"
  CADDY_STATE="起動済み"
else
  systemctl stop caddy 2>/dev/null || true
  echo "  DNS 未一致（$DOMAIN -> ${DNS_IP:-未解決} / このホスト -> ${MY_IP:-不明}）"
  echo "  Caddy は起動しない。A レコードを向けてから再実行すること"
  CADDY_STATE="未起動（DNS 待ち）"
fi

echo "▶ 7/7 完了"
cat <<EOF

  このインスタンスの IP : ${MY_IP:-不明}
  Caddy                : $CADDY_STATE

次の手順:
  1. DNS に A レコードを追加   $DOMAIN -> ${MY_IP:-<このインスタンスのIP>}
  2. 手元から配備              ./deploy/deploy.sh ubuntu@${MY_IP:-<IP>}
  3. DNS 反映後、もう一度      sudo bash setup.sh $DOMAIN
  4. 確認                      curl -I https://$DOMAIN/
EOF
