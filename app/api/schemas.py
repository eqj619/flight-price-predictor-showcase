"""API の入出力スキーマ。

匿名の公開エンドポイントが未検証の入力（空港コード・日付・トークン）を
受けるので、FastAPI を選んだ理由の半分はここにある。ハンドラに素の dict を
渡さず、境界で必ず pydantic を通す。
"""
import logging
import re
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from app import airports, clock
from app.config import get_settings

_AIRPORT = re.compile(r"^[A-Z]{3}$")


def _validated_airport(value) -> str:
    """3文字であることに加え、**実在する空港かどうかまで見る。**

    形式だけの検証では NAR（NRT の打ち間違い）・MIL（ミラノの都市コードで
    あって空港ではない）・CST（該当なし）がそのまま収集対象になり、出発日
    まで毎日失敗し続ける。利用者には「取れないモニター」が残るだけで、
    理由も伝わらない。

    都市コード（TYO, OSA …）は**空港ではないので拒否する**が、どの空港を
    指せばよいかまで伝える。ここで黙って代表空港に読み替えると、利用者が
    頼んでいないルートを監視することになる。
    """
    if not isinstance(value, str):
        raise ValueError("空港コードは文字列で指定してください")
    code = value.strip().upper()
    if not _AIRPORT.match(code):
        raise ValueError("空港コードは3文字のIATAコードで指定してください")
    if airports.exists(code):
        return code
    members = airports.metro_members(code)
    if members:
        names = " / ".join(f"{m}（{airports.get(m)['name']}）" for m in members)
        raise ValueError(
            f"{code} は都市コードです。空港を選んでください: {names}")
    raise ValueError(
        f"{code} という空港コードは見つかりません。"
        "定期便のある空港の3文字コードを指定してください")
# 完全な RFC 準拠は狙わない（誤入力を弾ければよい）。厳密にやりたく
# なったら email-validator を足す。
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


logger = logging.getLogger(__name__)


class WatchCreate(BaseModel):
    origin: str
    destination: str
    departure_date: date
    # 復路の日付。**None なら片道。** 往復は別の量として収集・保存する
    # （往復運賃は片道2本の合成では出せない。実測で片道の 1.85倍、
    # ただし 1.21〜2.45 とばらつく）。
    return_date: Optional[date] = None
    airline_filter: list[str] = Field(default_factory=list, max_length=20)
    # 近接空港ルール（2.3）。既定は有効。
    # **既定はオフ（2026-08-14 に変更）。** 1モニターが最大4ルートに
    # 展開されるため、ルート枠の消費が利用者数の 2.4 倍になっていた。
    # 価値のある機能なので消さず、設定タブで各自が有効にする形にした。
    nearby_airports: bool = False
    # 通知は今回のスコープ外。列だけ用意してある（Phase 5 のスコープ外項目）。
    notify_email: Optional[str] = None

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def _normalize_airport(cls, value):
        return _validated_airport(value)

    @field_validator("airline_filter")
    @classmethod
    def _clean_airlines(cls, value):
        return [a.strip() for a in value if a and a.strip()]

    @field_validator("notify_email")
    @classmethod
    def _check_email(cls, value):
        if value in (None, ""):
            return None
        if not _EMAIL.match(value):
            raise ValueError("メールアドレスの形式が不正です")
        return value

    @model_validator(mode="after")
    def _check_route_and_date(self):
        if self.origin == self.destination:
            raise ValueError("出発空港と到着空港が同じです")

        settings = get_settings()
        today = clock.today()
        if self.departure_date < today:
            raise ValueError("出発日が過去です")
        if self.departure_date > today + timedelta(days=settings.max_departure_days_ahead):
            raise ValueError(
                f"出発日は今日から{settings.max_departure_days_ahead}日以内で指定してください"
            )
        if self.return_date is not None:
            if self.return_date <= self.departure_date:
                raise ValueError("復路は出発日より後の日付で指定してください")
            if self.return_date > today + timedelta(days=settings.max_departure_days_ahead):
                raise ValueError(
                    f"復路は今日から{settings.max_departure_days_ahead}日以内で指定してください"
                )
        return self


class WatchUpdate(BaseModel):
    """モニター設定の変更。

    **省略した項目は変更しない。** 既定値を持たせて「送られなければ既定に
    戻す」にすると、航空会社だけ変えたいときに近接空港ルールまで戻ってしまう。
    """

    # 空リストは「全社に戻す」という明示の指示。None は「変更しない」。
    airline_filter: Optional[list[str]] = Field(default=None, max_length=20)
    nearby_airports: Optional[bool] = None
    # 総所要時間の上限（分）。**0 = 上限なし**、None = 変更しない。
    # 「上限なし」を None で表すと「変更しない」と区別できないため 0 を使う。
    # 上限 6000 分（100時間）は入力の歯止めで、意味のある値ではない。
    max_total_minutes: Optional[int] = Field(default=None, ge=0, le=6000)

    @field_validator("airline_filter")
    @classmethod
    def _clean_airlines(cls, value):
        if value is None:
            return None
        # 重複と空白だけの項目を落とす。順序は入力どおりに保つ。
        seen = set()
        cleaned = []
        for raw in value:
            name = (raw or "").strip()
            if name and name not in seen:
                seen.add(name)
                cleaned.append(name)
        return cleaned


class RouteRef(BaseModel):
    origin: str
    destination: str
    # 2.3 の近接空港ルールで自動追加されたルートか。
    # UI で「LAX 着（陸路で SAN へ）」と説明するために必要。
    nearby: bool


class WatchCreated(BaseModel):
    token: str
    url: str
    origin: str
    destination: str
    departure_date: date
    # 片道なら None。
    return_date: Optional[date] = None
    expires_at: date
    routes: list[RouteRef]


# 通知の宛先として認めるホスト。**ここに無いものは受け取らない。**
#
# 2026-09-14 まで、検証は `startswith("https://")` だけだった。この受け口は
# 認証なしなので、任意の https URL を登録して**我々のサーバから好きな宛先へ
# POST させられた**（SSRF）。出発日を「今日 + 52日」にすれば値上がり通知の
# 条件が翌日に成立するので、発火も攻撃者が仕込める。
#
# 実在の購読（2026-09-14 時点で3件）は fcm.googleapis.com と
# web.push.apple.com のみ。下の一覧はそれを含む主要ブラウザの宛先。
#
# **ブラウザが新しい宛先を使い始めたら、ここに足す必要がある。** 落ちたときに
# 気づけるよう、拒否したホストは警告に残す（URL 全体は残さない — 末尾が
# 購読の秘密なので）。
_PUSH_HOSTS = (
    "fcm.googleapis.com",                 # Chrome / Edge / Android
    "android.googleapis.com",             # 旧 Chrome
    "updates.push.services.mozilla.com",  # Firefox
    "push.apple.com",                     # Safari（web.push.apple.com 等）
    "notify.windows.com",                 # Windows（*.notify.windows.com）
)


def _is_known_push_host(host: str) -> bool:
    """既知のプッシュサービスか。サブドメインは認める。

    `web.push.apple.com` や `par02p.notify.windows.com` のように、実際の
    宛先はサブドメインで来る。
    """
    return any(host == known or host.endswith("." + known)
               for known in _PUSH_HOSTS)


class PushSubscribe(BaseModel):
    """ブラウザの PushSubscription をそのまま受ける形。

    endpoint はプッシュサービスの URL。**任意の URL を受け付けない** —
    既知のプッシュサービス（`_PUSH_HOSTS`）宛てだけを通す。ここを緩めると、
    我々のサーバから任意の宛先へ POST させられる（SSRF）。
    """

    endpoint: str = Field(min_length=20, max_length=1000)
    p256dh: str = Field(min_length=16, max_length=200)
    auth: str = Field(min_length=8, max_length=100)
    # どのモニターの通知を受け取るか。
    watch_token: str = Field(min_length=8, max_length=64)
    # 通知の本文はサーバが作るので、表示言語を受け取る必要がある。
    lang: str = Field(default="en", pattern="^(ja|en)$")

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("endpoint は https でなければなりません")
        # **urlparse に任せる。** 自前で切り出すと
        # `https://fcm.googleapis.com@evil.example/` のような形を取り違える
        # （この場合ホストは evil.example）。
        host = (urlparse(value).hostname or "").lower()
        if not _is_known_push_host(host):
            logger.warning("未知のプッシュサービスを拒否: %s", host or "(空)")
            raise ValueError("endpoint が既知のプッシュサービスではありません")
        return value


class PushUnsubscribe(BaseModel):
    endpoint: str = Field(min_length=20, max_length=1000)
    # 省略すると購読そのものを消す。指定するとそのモニターだけ外す。
    watch_token: Optional[str] = Field(default=None, min_length=8, max_length=64)
