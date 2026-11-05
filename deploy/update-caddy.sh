#!/usr/bin/env bash
#
# 稼働中のインスタンスへ Caddyfile の変更を反映する。
#
#   ./deploy/update-caddy.sh ubuntu@<IP> <key.pem>
#
# **deploy.sh は Caddyfile を配らない。** アプリのコードだけを rsync する。
# TLS の入口を毎回の配備で触ると、アプリの小さな変更でサイト全体を落とす
# 余地が生まれるため、意図的に分けてある。
#
# リポジトリの deploy/Caddyfile は `__DOMAIN__` のままのテンプレート。
# **そのまま cp すると本番のドメイン定義が消えてサイトが落ちる**
# （2026-08-11 に実際に落とした）。ここでドメインを稼働中の設定から
# 読み取って差し込み、失敗したら自動で元に戻す。
set -euo pipefail

HOST=${1:-}
KEY=${2:-}
if [[ -z "$HOST" ]]; then
  echo "usage: $0 <user@host> [ssh-key]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_OPTS=(-o StrictHostKeyChecking=no)
[[ -n "$KEY" ]] && SSH_OPTS+=(-i "$KEY")

ssh_run() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

echo "▶ 1/5 稼働中のドメインを読み取る"
# テンプレートを当てる前に、いま動いている設定から実ドメインを取る。
# 引数で受け取る形にしないのは、打ち間違いがそのまま切り替えになるため。
DOMAIN=$(ssh_run "sudo grep -oE '^[a-z0-9.-]+\.[a-z]+ \{' /etc/caddy/Caddyfile | head -1 | tr -d ' {'")
if [[ -z "$DOMAIN" || "$DOMAIN" == *"__DOMAIN__"* ]]; then
  echo "  ✗ 稼働中の Caddyfile からドメインを特定できない。手動で確認すること" >&2
  exit 1
fi
echo "  $DOMAIN"

echo "▶ 2/5 テンプレートにドメインを差し込んで転送"
sed "s/__DOMAIN__/$DOMAIN/g" "$SCRIPT_DIR/Caddyfile" \
  | ssh_run "cat > /tmp/Caddyfile.new"
ssh_run "grep -q '__DOMAIN__' /tmp/Caddyfile.new && { echo '  ✗ 置換漏れ' >&2; exit 1; } || true"

echo "▶ 3/5 差分"
ssh_run "sudo diff /etc/caddy/Caddyfile /tmp/Caddyfile.new || true"

echo "▶ 4/5 検証して反映"
ssh_run "
set -e
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak
sudo cp /tmp/Caddyfile.new /etc/caddy/Caddyfile && rm -f /tmp/Caddyfile.new
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
# validate を root で走らせるとログファイルを root 所有で作ることがある。
# ディレクトリだけでなく再帰的に直す（これを怠って起動不能にしたことがある）。
sudo chown -R caddy:caddy /var/log/caddy
sudo systemctl reload caddy
"

echo "▶ 5/5 疎通を確認（失敗したら自動で戻す）"
sleep 3
for attempt in 1 2 3 4 5; do
  code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://$DOMAIN/" || echo 000)
  [[ "$code" == "200" ]] && { echo "  HTTP $code ✅ 反映完了"; exit 0; }
  echo "  HTTP $code（再試行 $attempt/5）"
  sleep 5
done

echo "  ✗ 応答が戻らない。バックアップへ復旧する" >&2
ssh_run "sudo cp /etc/caddy/Caddyfile.bak /etc/caddy/Caddyfile && sudo systemctl reload caddy"
sleep 3
echo "  復旧後: HTTP $(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://$DOMAIN/")" >&2
exit 1
