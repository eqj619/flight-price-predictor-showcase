"""価格データソースの抽象。

fast_flights は Google Flights の非公式スクレイパで、いつ壊れても不思議はない。
将来 Amadeus 等の公式 API に差し替えられるよう、collector.py は必ずこの ABC
越しにしか価格を取らない（collector.py に fast_flights を import しない）。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence


@dataclass(frozen=True)
class Quote:
    """1便分の運賃。データソース非依存の中間表現。

    Snapshot と似ているが別物にしてある。Snapshot は「いつ観測したか」を
    含む永続化の都合を持つのに対し、Quote は取得結果そのもの。

    価格の下の項目は、取得できたら必ず持ち帰る。**後から遡って取れない**
    ので、いま使わなくても捨てない（深夜便が安い・乗継が長いほど安い、
    といった効果は価格モデルに効く）。取れないデータソースもあるため
    すべて任意。

    **往復のときは、価格と便の情報で指す範囲が違う。**

        price_usd   … 往復の合計
        それ以外     … 往路だけ

    Google Flights が往路選択の画面でそう見せているためで、復路の便は応答に
    入っていない（2026-08-19 実測）。stops / via_airport / 所要時間 / 機材 /
    CO2 はすべて往路のもの。**この非対称は取得元の都合であって、直せない。**
    使う側が往復かどうかを見て解釈すること。
    """
    airline: str
    stops: int
    via_airport: Optional[str]
    price_usd: float
    departure_time: Optional[str] = None   # 'YYYY-MM-DDTHH:MM'（現地）
    arrival_time: Optional[str] = None
    air_minutes: Optional[int] = None      # 飛行時間の合計（乗継時間を含まない）
    total_minutes: Optional[int] = None    # 総所要（飛行 + 乗継）
    plane_types: Optional[str] = None
    carbon_g: Optional[int] = None


@dataclass(frozen=True)
class HistoryPoint:
    """過去のある日の、その検索条件での最安値。

    **Quote と違って便を特定しない。** 取得元が持っている履歴は
    「この検索での最安がいくらだったか」という粒度しかない。
    """
    observed_date: date
    price_usd: float


class ProviderError(Exception):
    """リトライしても回復しなかった取得失敗。

    **「該当便なし」はエラーではない。** 実装は空リストを返すこと。
    この区別を守らないと、そもそも直行便が飛んでいない路線で fetch_errors が
    埋め尽くされ、本当の障害が見えなくなる。
    """


class PriceProvider(ABC):
    """価格取得の入口。"""

    name: str = "abstract"

    @abstractmethod
    def fetch(self, origin: str, destination: str, departure_date: date,
              stops: int,
              airlines: Optional[Sequence[str]] = None,
              return_date: Optional[date] = None) -> list[Quote]:
        """1回の検索。指定した経由回数「ちょうど」の便を返す。

        `return_date` を渡すと往復を検索する。**既定は None = 片道**なので、
        片道しか扱わなかった頃の呼び出しはそのまま通る。

        契約:

        - `stops` は「以下」ではなく「ちょうど」。データソースのフィルタ結果を
          鵜呑みにせず、実際のセグメント数で検証してから返すこと。
          **往復のときは往路についてのみ検証できる**（復路は応答に無い）
        - 該当便が無いときは `[]` を返す（例外にしない）
        - 復旧不能な失敗のときだけ `ProviderError` を送出する
        - 呼び出し間隔（ポライトネス）とリトライは実装側の責務。collector は
          待ち時間を知らない — テスト用のフェイクは一切待たずに済む
        """

    def fetch_history(self, origin: str, destination: str,
                      departure_date: date,
                      return_date: Optional[date] = None) -> list[HistoryPoint]:
        """取得元が持っている過去の価格を、取れるだけ返す。

        **既定は「対応していない」＝空リスト。** 抽象メソッドにしないのは、
        これを提供できるデータソースのほうが例外的だから。公式 API へ
        差し替えたときに、実装を強制されて空実装を書かされるのは筋が悪い。

        取れなかったときも例外にせず `[]` を返すこと。**これは補助的な
        機能で、失敗しても日次収集を止めてはいけない。**
        """
        return []
