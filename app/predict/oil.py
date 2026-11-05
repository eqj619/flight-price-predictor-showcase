"""原油価格（Brent 月次、ラグ付き）の予測への織り込み。

**初期値は係数 0 = 無効。** 実装だけしておいて有効化しない理由:

モニター期間は最長でも60日程度で、その間の原油価格の寄与はほぼ定数になる。
定数は較正のスケール項（observed / shape）に丸ごと吸収されるため、係数を
入れても予測の形は変わらず、EIA の障害という新しい失敗要因が増えるだけに
なる。実測が貯まって「原油変動が運賃に何ヶ月遅れでどれだけ効くか」を
推定できてから有効化する。

したがって `oil_coefficient == 0` のときは **ネットワークアクセスを一切
行わない**。API キーが無くても動く。
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Optional

from app.config import Settings, get_settings

# EIA API v2。RBRTE = Europe Brent Spot Price FOB (USD/bbl)。
_EIA_URL = "https://api.eia.gov/v2/petroleum/pri/spt/data/"
_EIA_SERIES = "RBRTE"
_TIMEOUT_SECONDS = 10


def _lagged_month(as_of: date, lag_months: int) -> str:
    """as_of から lag_months ヶ月前の 'YYYY-MM'。"""
    total = as_of.year * 12 + (as_of.month - 1) - lag_months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


class OilAdapter:
    """Brent のラグ付き月次価格から、予測に掛ける倍率を出す。

    どんな失敗（キー未設定・通信断・レスポンス変化）でも例外を投げず 1.0 を
    返す。原油の取得失敗で価格予測そのものが止まるのは割に合わない。
    """

    def __init__(self, settings: Optional[Settings] = None, opener=None):
        self._settings = settings or get_settings()
        # テストで差し替えるための注入点。実 API は叩かせない。
        self._opener = opener or urllib.request.urlopen
        self._cache: dict[str, Optional[float]] = {}

    # ------------------------------------------------------------------

    def adjustment_factor(self, as_of: date) -> float:
        """予測価格に掛ける倍率。無効時・取得失敗時は 1.0。"""
        coefficient = self._settings.oil_coefficient
        if coefficient == 0:
            # 無効時はここで打ち切る。ネットワークにも触らない。
            return 1.0

        brent = self.lagged_brent(as_of)
        if brent is None or self._settings.oil_baseline_usd <= 0:
            return 1.0

        deviation = brent / self._settings.oil_baseline_usd - 1.0
        return 1.0 + coefficient * deviation

    def lagged_brent(self, as_of: date) -> Optional[float]:
        """ラグ付きの Brent 月次平均（USD/bbl）。取得できなければ None。"""
        month = _lagged_month(as_of, self._settings.oil_lag_months)
        if month in self._cache:
            return self._cache[month]
        value = self._fetch(month)
        self._cache[month] = value
        return value

    # ------------------------------------------------------------------

    def _fetch(self, month: str) -> Optional[float]:
        api_key = self._settings.eia_api_key
        if not api_key:
            # キーはコードに書かず .env / Secrets Manager から。
            # 無ければ静かに無効化する。
            return None

        params = {
            "api_key": api_key,
            "frequency": "monthly",
            "data[0]": "value",
            "facets[series][]": _EIA_SERIES,
            "start": month,
            "end": month,
        }
        url = f"{_EIA_URL}?{urllib.parse.urlencode(params)}"
        try:
            with self._opener(url, timeout=_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            rows = payload["response"]["data"]
            return float(rows[0]["value"]) if rows else None
        except (urllib.error.URLError, OSError, ValueError,
                KeyError, IndexError, TypeError):
            # レスポンス形式が変わっても予測を止めない。
            return None
