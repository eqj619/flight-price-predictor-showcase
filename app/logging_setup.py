"""アプリのログ設定。

これが無いと `app.*` ロガーにハンドラが付かず、日次ジョブの結果も例外も
どこにも出ない。無人で回るジョブが黙って失敗するのが一番困る壊れ方なので、
標準出力とファイルの両方に出す（既存 flight-price-monitor の run_daily.py が
同じ理由で logging を自前設定していた）。

トークンの伏字フィルタも必ずここで掛ける（app/security.py）。
"""
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional

from app.config import get_settings
from app.security import install_log_redaction

_configured = False


def configure_logging(level: Optional[str] = None) -> None:
    """`app.*` ロガーに標準出力とファイルのハンドラを付ける。冪等。"""
    global _configured
    if _configured:
        return

    settings = get_settings()
    logger = logging.getLogger("app")
    logger.setLevel(level or settings.log_level)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    try:
        log_path = settings.db_path.parent / "app.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # 常駐プロセスなので必ず回す。FileHandler のままだと、日次ジョブの
        # ログが何年も積み上がってディスクを食い潰す。
        file_handler = RotatingFileHandler(
            log_path, maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count, encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # 書けない場所でもアプリは動くべき。標準出力だけで続行する。
        logger.warning("ログファイルを開けませんでした。標準出力のみに出力します。")

    install_log_redaction()
    _configured = True
