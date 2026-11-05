#!/usr/bin/env bash
#
# 手元からコードを配備する。**何度実行してもよい。**
#
#   ./deploy/deploy.sh ubuntu@44.246.136.114
#   ./deploy/deploy.sh ubuntu@44.246.136.114 ~/.ssh/lightsail.pem
#
# GitHub のリポジトリは private なので、サーバに deploy key を置くのではなく
# 手元から rsync する。サーバに認証情報を持たせないで済む。
set -euo pipefail

TARGET="${1:-}"
SSH_KEY="${2:-}"
if [ -z "$TARGET" ]; then
  echo "usage: ./deploy/deploy.sh <user@host> [ssh-key]" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/fpp

# 空配列にしない。macOS の bash 3.2 は set -u のもとで "${arr[@]}" を
# 未定義変数として扱うため、鍵を渡さない呼び方で落ちる。
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
[ -n "$SSH_KEY" ] && SSH_OPTS+=(-i "$SSH_KEY")

ssh_run() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }

# /etc/fpp/env を読んでからアプリを叩く。`xargs` に流すやり方だと
# コメント行や値の空白で壊れる。
# `cd` を忘れると `python -m app.cli` がモジュールを見つけられない。
remote_app() {
  ssh_run "sudo -u fpp bash -c 'cd $APP_DIR && set -a && . /etc/fpp/env && set +a && exec .venv/bin/python -m app.cli $*'"
}

echo "▶ 1/4 コードを転送"
# data/ は転送しない。**サーバ上の価格履歴を手元の開発用 DB で上書きしない。**
rsync -az --delete \
  --rsync-path="sudo rsync" \
  --exclude='.venv/' --exclude='data/' --exclude='.git/' \
  --exclude='__pycache__/' --exclude='.pytest_cache/' --exclude='.env' \
  -e "ssh ${SSH_OPTS[*]}" \
  "$REPO_ROOT/app" "$REPO_ROOT/static" "$REPO_ROOT/deploy" \
  "$REPO_ROOT/scripts" "$REPO_ROOT/dashboard" \
  "$REPO_ROOT/requirements.txt" \
  "$TARGET:$APP_DIR/"

echo "▶ 2/4 依存を更新"
# fpp はホームディレクトリを持たないので --no-cache-dir にする
# （キャッシュを書けずに警告が出るだけで、ディスクも節約になる）。
ssh_run "sudo -u fpp bash -c '
  cd $APP_DIR
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q --no-cache-dir --upgrade pip
  .venv/bin/pip install -q --no-cache-dir -r requirements.txt
'"

echo "▶ 3/4 スキーマを適用（冪等）"
remote_app init-db

echo "▶ 4/4 再起動"
ssh_run "sudo chown -R fpp:fpp $APP_DIR && sudo systemctl restart flight-price-predictor"
sleep 3
ssh_run "systemctl is-active flight-price-predictor"
remote_app status

echo "✅ 配備完了"
