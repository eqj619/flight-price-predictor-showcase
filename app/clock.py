"""アプリの「今日」。

`date.today()` を直接呼ぶとサーバの OS タイムゾーンに引きずられる。日次収集の
check_date が日付をまたぐ瞬間が運用環境ごとに変わってしまい、UTC のサーバでは
利用者にとっての夕方に日付が変わる。基準は必ず設定の TIMEZONE に置く。
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings


def now() -> datetime:
    return datetime.now(ZoneInfo(get_settings().timezone))


def today() -> date:
    return now().date()
