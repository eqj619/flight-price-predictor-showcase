"""匿名運用まわりの安全側の道具。

トークンは capability（URL を知っている人＝閲覧・編集できる）なので、
**URL がどこにも平文で残らないこと**が匿名マルチテナントの前提になる。
アクセスログ・エラーログ・外部送信先の3つを塞ぐ必要がある。

- アクセスログ / エラーログ -> `redact()` と `install_log_redaction()`
- 外部送信先 -> Chart.js を CDN ではなく static/vendor に置き、
  `Referrer-Policy: no-referrer` を付ける（CDN への Referer ヘッダには
  `/w/{token}` がそのまま載る）
"""
import hashlib
import logging
import re
import secrets
from pathlib import Path
from typing import Optional

from app.config import get_settings

# ログに出てはいけないパス。トークン部分だけを伏せる。
#
# `/api/watches/` ではなく `/watches/` で書いてあるのは、
# `/api/collections/{一覧}/watches/{モニター}` のように**1つのパスに
# トークンが2つ載る**形があるため。前方の接頭辞まで含めて書くと、
# 後ろ側のトークンが伏字にならない。
_TOKEN_PATH = re.compile(r"(/w/|/l/|/watches/|/collections/)[A-Za-z0-9_\-]{8,}")
_REDACTED = r"\1<token>"

_SALT_FILENAME = ".ip_salt"


def redact(text: str) -> str:
    """文字列中のトークンを伏せる。"""
    return _TOKEN_PATH.sub(_REDACTED, text)


class _RedactTokens(logging.Filter):
    """ログレコードのメッセージからトークンを消すフィルタ。

    uvicorn のアクセスログは `"GET /w/abc123 HTTP/1.1" 200` の形で
    パスを args に持つので、args 側も見る必要がある。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True


def install_log_redaction() -> None:
    """全ロガーにトークン伏字フィルタを掛ける。

    uvicorn.access だけでは足りない。例外が出たときのトレースバックにも
    リクエストパスが載る経路があるため、root にも掛ける。
    """
    filter_ = _RedactTokens()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "app"):
        logging.getLogger(name).addFilter(filter_)


# ----------------------------------------------------------------------
# IP のハッシュ化
# ----------------------------------------------------------------------

def _salt_path() -> Path:
    return get_settings().db_path.parent / _SALT_FILENAME


def _load_or_create_salt() -> str:
    """ソルトを取り出す。無ければ生成して保存する。

    ソルト無しの SHA-256 では意味がない。IPv4 は全空間が43億通りしかなく、
    総当たりで元の IP に戻せてしまう。

    運用者に .env の設定を強制すると、設定し忘れたまま「ソルト空」で
    動くのが一番ありそうな失敗なので、既定では自動生成してファイルに残す。
    """
    settings = get_settings()
    if settings.ip_hash_salt:
        return settings.ip_hash_salt

    path = _salt_path()
    if path.exists():
        return path.read_text(encoding="utf-8").strip()

    salt = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(salt, encoding="utf-8")
    path.chmod(0o600)
    return salt


def ip_hash(ip: Optional[str]) -> Optional[str]:
    """レート制限用の IP ハッシュ。生の IP は保存しない。"""
    if not ip:
        return None
    return hashlib.sha256(f"{_load_or_create_salt()}|{ip}".encode()).hexdigest()
