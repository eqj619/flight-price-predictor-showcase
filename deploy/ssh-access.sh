#!/usr/bin/env bash
#
# SSH(22) は普段閉じておき、配備のときだけ開ける。
#
#   ./deploy/ssh-access.sh status   いまの公開範囲を見る
#   ./deploy/ssh-access.sh open     この端末のグローバル IP からの 22 を開ける
#   ./deploy/ssh-access.sh close    22 をブラウザ SSH のみに戻す
#
# 閉じた状態でも Lightsail コンソールのブラウザ SSH は使える
# （cidrListAliases の "lightsail-connect" が AWS 側の接続元を表す）。
# 締め出される心配はないので、作業後は必ず close すること。
#
# **put-instance-public-ports は「渡した集合で全置換」する。**
# 22 を触るときに 80/443 を一緒に渡さないと Web が落ちる。だからこの
# スクリプトは常に3つまとめて送る。手で aws コマンドを叩かないこと。
set -euo pipefail

REGION="${LIGHTSAIL_REGION:-us-west-2}"
INSTANCE="${LIGHTSAIL_INSTANCE:-flight-price-predictor}"
export AWS_PAGER=""

# 80 は Let's Encrypt の HTTP-01 チャレンジに要る（Caddy が HTTPS へ転送する）。
WEB_PORTS='
  {"fromPort":80,"toPort":80,"protocol":"tcp",
   "cidrs":["0.0.0.0/0"],"ipv6Cidrs":["::/0"]},
  {"fromPort":443,"toPort":443,"protocol":"tcp",
   "cidrs":["0.0.0.0/0"],"ipv6Cidrs":["::/0"]}
'

apply() {   # $1 = 22 番の portInfo（JSON）
  aws lightsail put-instance-public-ports --region "$REGION" \
    --instance-name "$INSTANCE" \
    --port-infos "[$1,$WEB_PORTS]" \
    --query 'operation.status' --output text >/dev/null
}

show() {
  aws lightsail get-instance-port-states --region "$REGION" \
    --instance-name "$INSTANCE" \
    --query 'sort_by(portStates,&fromPort)[].{port:fromPort,cidrs:cidrs,browser:cidrListAliases}' \
    --output table
}

case "${1:-status}" in
  open)
    MYIP="$(curl -fsS --max-time 10 https://checkip.amazonaws.com)"
    [ -n "$MYIP" ] || { echo "❌ グローバル IP を取得できません" >&2; exit 1; }
    apply "{\"fromPort\":22,\"toPort\":22,\"protocol\":\"tcp\",
            \"cidrs\":[\"$MYIP/32\"],\"cidrListAliases\":[\"lightsail-connect\"]}"
    echo "🔓 22 を $MYIP/32 に開きました。**作業後に close すること。**"
    show
    ;;
  close)
    apply '{"fromPort":22,"toPort":22,"protocol":"tcp",
            "cidrs":[],"ipv6Cidrs":[],"cidrListAliases":["lightsail-connect"]}'
    echo "🔒 22 を閉じました（ブラウザ SSH のみ）。"
    show
    ;;
  status)
    show
    ;;
  *)
    echo "usage: $0 {status|open|close}" >&2
    exit 1
    ;;
esac
