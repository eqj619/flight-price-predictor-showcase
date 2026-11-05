"""fast_flights（Google Flights スクレイパ）による PriceProvider 実装。

既存 flight-price-monitor の collector.py から、実運用で判明した3点を引き継ぐ。
どれも「知らないと静かに壊れる」類のものなので、理由をコメントに残す。

1. bare TypeError は「該当便なし」であってエラーではない
2. max_stops フィルタの結果は信用せず、実セグメント数で検証する
3. リトライ・指数バックオフ・呼び出し間隔のランダム化
"""
import json
import random
import time
import traceback
from datetime import date, datetime, timezone
from typing import Optional, Sequence

from fast_flights import FlightQuery, Passengers, create_query
from fast_flights.exceptions import FlightsNotFound
from fast_flights.fetcher import fetch_flights_html
from selectolax.lexbor import LexborHTMLParser

from app.collect.provider import (
    HistoryPoint, PriceProvider, ProviderError, Quote,
)
from app.config import Settings, get_settings

# payload のどこに何が入るか。**fast_flights は [3] と [7] しか読んでいない。**
# ここは公式に文書化されたものではなく、応答を実測して突き止めた位置。
HISTORY_BLOCK = 5        # 価格の判定材料
HISTORY_SERIES = 10      # その中の時系列
# 価格として受け入れる範囲。桁違いの値が混ざったときに黙って取り込まない。
PRICE_MIN_USD, PRICE_MAX_USD = 10.0, 100_000.0


def parse_history(html: str) -> list[HistoryPoint]:
    """検索結果の HTML から価格履歴を取り出す。**取れなければ空を返す。**

    `fetch_history` から切り出してあるのは、ネットワークなしで試せるように
    するため。抽出が壊れたかどうかは、保存した HTML を1本流せば分かる。

    エポックミリ秒は UTC で解釈する。**現地時刻に直さない。** 1日1点の
    粗い系列に対して、時差ぶんの前後を気にしても精度は上がらず、
    空港ごとのタイムゾーン表を持ち込む害のほうが大きい。
    """
    try:
        script = LexborHTMLParser(html).css_first(r"script.ds\:1")
        payload = json.loads(script.text().split("data:", 1)[1].rsplit(",", 1)[0])
        series = payload[HISTORY_BLOCK][HISTORY_SERIES][0]
    except Exception:
        return []

    points = []
    seen = set()
    for pair in series or []:
        try:
            millis, price = pair[0], float(pair[1])
            observed = datetime.fromtimestamp(millis / 1000, tz=timezone.utc).date()
        except (TypeError, ValueError, IndexError, OSError, OverflowError):
            continue
        # 同じ日が2回来たら先勝ち。UNIQUE 制約とぶつける前にここで潰す。
        if observed in seen or not (PRICE_MIN_USD <= price <= PRICE_MAX_USD):
            continue
        seen.add(observed)
        points.append(HistoryPoint(observed_date=observed, price_usd=price))

    points.sort(key=lambda p: p.observed_date)
    return points


def _via_airport(flight_group) -> Optional[str]:
    """経由地（最初のセグメントの到着空港）。直行なら None。"""
    segments = flight_group.flights
    if len(segments) <= 1:
        return None
    return segments[0].to_airport.code


def _hhmm(time_parts) -> Optional[tuple[int, int]]:
    """時刻のタプルを (時, 分) にする。**0 は省かれて来る。**

    取得元は 0 の成分を落とす。2026-09-08 の実測（SAN→HND / LAX→NRT の
    18便）で確かめた並び:

        [6, 15]      06:15
        [17]         17:00   ← 分が 0 なので落ちている
        [None, 50]   00:50   ← 時が 0 なので None になっている
        [23]         23:00
        [None, 35]   00:35

    `hh, mm = stamp.time` と決め打っていたため、**正時ちょうどの便と
    深夜0時台の便は時刻を失っていた**（要素数が合わずに ValueError、
    あるいは None を書式化できず TypeError）。実測18便のうち6便、
    およそ3割がこれに当たる。

    時刻を失うと総所要も出せない。総所要が無い行は「不明」として
    24時間の上限をすり抜けるので、**長すぎる便だけが選ばれて残る**という
    逆向きの偏りまで生んでいた。
    """
    if not isinstance(time_parts, (list, tuple)) or not time_parts:
        return None
    if len(time_parts) > 2:
        return None                     # 知らない形。**推測しない。**
    parts = list(time_parts) + [0] * (2 - len(time_parts))
    hh, mm = (0 if v is None else v for v in parts)
    if not (isinstance(hh, int) and isinstance(mm, int)):
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def _ymd(date_parts) -> Optional[tuple[int, int, int]]:
    """日付のタプルを (年, 月, 日) にする。月日は 0 にならないので省かれない。"""
    try:
        y, m, d = date_parts
        if not all(isinstance(v, int) for v in (y, m, d)):
            return None
        return y, m, d
    except (TypeError, ValueError):
        return None


def _iso_local(stamp) -> Optional[str]:
    """fast_flights の SimpleDatetime を 'YYYY-MM-DDTHH:MM' にする。

    date/time はタプルで来る。タイムゾーンは付いていない（各空港の現地時刻）
    ので、日時としてではなく**文字列として**保存する。UTC に正規化しようと
    すると、空港ごとのタイムゾーン表を持つことになり、そこで間違えるほうが
    害が大きい。
    """
    ymd = _ymd(getattr(stamp, "date", None))
    hhmm = _hhmm(getattr(stamp, "time", None))
    if ymd is None or hhmm is None:
        return None
    return "%04d-%02d-%02dT%02d:%02d" % (*ymd, *hhmm)


def _naive(stamp) -> Optional[datetime]:
    """SimpleDatetime を素の datetime にする（タイムゾーンは付けない）。"""
    ymd = _ymd(getattr(stamp, "date", None))
    hhmm = _hhmm(getattr(stamp, "time", None))
    if ymd is None or hhmm is None:
        return None
    try:
        return datetime(*ymd, *hhmm)
    except ValueError:
        return None


def _layover_minutes(segments) -> Optional[int]:
    """乗継時間の合計。

    **同じ空港での「到着 → 次の出発」なので、現地時刻のまま引いて正しい。**
    出発地と到着地の引き算はこうはいかない（タイムゾーンと日付変更線を
    またぐため、NRT 18:35 発 → YYC 12:45 着 のように「戻る」）。
    総所要を出すのに到着時刻 − 出発時刻を使ってはいけないのはこのため。
    """
    total = 0
    for i in range(len(segments) - 1):
        arrive = _naive(getattr(segments[i], "arrival", None))
        depart = _naive(getattr(segments[i + 1], "departure", None))
        if arrive is None or depart is None:
            return None
        gap = int((depart - arrive).total_seconds() // 60)
        if gap < 0:
            # 現地時刻が逆転するのは、乗継が空港をまたぐ（都市内の別空港）か
            # パースがずれた場合。推測せず「不明」にする。
            return None
        total += gap
    return total


def _flight_details(flight_group) -> dict:
    """価格以外で取得できているものを拾う。

    **後から遡って取得できない。** いま予測に使っていなくても捨てない。
    パーサの都合で欠けることがあるので、1項目でも失敗したら値だけ None に
    して、収集そのものは続ける。
    """
    segments = getattr(flight_group, "flights", None) or []
    details = {"departure_time": None, "arrival_time": None,
               "air_minutes": None, "total_minutes": None,
               "plane_types": None, "carbon_g": None}
    if not segments:
        return details

    details["departure_time"] = _iso_local(getattr(segments[0], "departure", None))
    details["arrival_time"] = _iso_local(getattr(segments[-1], "arrival", None))

    durations = [d for d in (getattr(s, "duration", None) for s in segments)
                 if isinstance(d, int)]
    if durations and len(durations) == len(segments):
        # 各区間の飛行時間の合計。乗継時間は含まない。
        details["air_minutes"] = sum(durations)

        # 総所要 = 飛行 + 乗継。ライブラリに総所要の項目が無いので自分で出す。
        # **収集時にしか計算できない**（区間の内訳を保存していないため）。
        layover = _layover_minutes(segments)
        if layover is not None:
            details["total_minutes"] = details["air_minutes"] + layover

    planes = [s.plane_type for s in segments if getattr(s, "plane_type", None)]
    if planes:
        details["plane_types"] = ", ".join(planes)

    carbon = getattr(flight_group, "carbon", None)
    if carbon is not None and isinstance(getattr(carbon, "emission", None), int):
        details["carbon_g"] = carbon.emission

    return details


# 便の一覧が入っている位置。history と同じく、応答を実測して突き止めたもの。
# 便の一覧は**1か所ではない。** 応答には同じ検索の便が2ブロックに分かれて
# 入っている。2026-09-08 に SAN→HND 2026-10-08 を取得して確かめた並び:
#
#   payload[3][0]  $576 LAX(United,ANA)  $676 LAX(United,ANA)  $983 HNL(Alaska)
#                  $1112 SFO(Alaska,JAL) $1135 SEA(Delta)
#   payload[2][0]  $627 ORD(American,JAL) $662 LAX(United) $690 SFO(United)
#                  $693 SFO(United) $816 LAX(Delta)
#
# 両方とも同じ出発日・同じ区間の便で、**安い便は片方に偏っていない。**
# ライブラリ（fast_flights）は [3][0] しか読まず、自前の解析に切り替えた
# 2026-09-02 に私はその読み方をそのまま引き写した。結果、毎日およそ半分の
# 便を捨てていた。便がどちらに入るかは日によって変わるので、同じ便が
# 現れたり消えたりし、価格の線が穴だらけになった。
#
# **[3] を先に置く。** 「便が無い」の判定はこのブロックが持っている
# （`_flight_groups` を参照）。順序を入れ替えるとその判定が変わる。
FLIGHT_BLOCKS = (3, 2)


def parse_flights(html: str) -> list:
    """検索結果の HTML から便を取り出す。**1便の欠損で全部を捨てない。**

    ライブラリの `parse_js` は `price = k[1][0][1]` を無防備に読む。価格を
    持たない便が結果に1件でも混ざると IndexError で落ち、**その区間の便が
    まるごと失われる。** 2026-09-01 に日本国内線と近距離アジア路線が一斉に
    これで落ちた（HND-KMJ / FUK-HND / HND-OKA / NRT-ICN など13ルート）。
    直前まで正常に取れていたので、上流が返す便の並びが変わったと見られる。

    ここでは便ごとに包んで、読めた便だけを返す。**取れる分は取る。**

    返すのはライブラリの `Flights` 型そのもの。呼び出し側（`_flight_details`
    など）が触る属性を変えないため、置き換えても他は一切直さなくてよい。

    ネットワークなしで試せるように、HTML を受け取る形にしてある
    （`parse_history` と同じ作法）。
    """
    from fast_flights.model import (Airport, CarbonEmission, Flights,
                                    SimpleDatetime, SingleFlight)

    # **「解析できた上で0件」と「解析できない」を混ぜないこと。**
    # 混ぜると、解析が全面的に壊れた日に「どの区間にも便が無い」と見え、
    # 誤り件数も collection_status も健全なまま警報が鳴らない。
    # 構造が読めないときは投げて、呼び出し側の再試行と誤り記録に載せる。
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise ValueError("応答に ds:1 のスクリプトが無い")
    data = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
    if data.rstrip().endswith("errorHasStatus: true"):
        # 取得元が明示的に「該当便なし」と言っている。
        raise FlightsNotFound("no flights found; received error")
    payload = json.loads(data)

    groups = _flight_groups(payload)
    if groups is None:
        return []          # 解析はできた。条件に合う便が無いだけ。

    out = []
    seen = set()
    for k in groups:
        try:
            price = float(k[1][0][1])
        except (TypeError, ValueError, IndexError, KeyError):
            # **値段の無い便。** 提携便など、Google が価格を出さないもの。
            # これ1件のために区間ごと落とさない。
            continue
        if not (PRICE_MIN_USD <= price <= PRICE_MAX_USD):
            continue

        # ブロックをまたいで同じ便が来ることがありうる。実測では重なりを
        # 見ていないが、**2度数えると平均が静かに歪む**ので先に落とす。
        mark = _identity(k)
        if mark is not None:
            if mark in seen:
                continue
            seen.add(mark)

        try:
            flight = k[0]
            segments = []
            for sf in flight[2]:
                segments.append(SingleFlight(
                    from_airport=Airport(code=sf[3], name=sf[4]),
                    to_airport=Airport(code=sf[6], name=sf[5]),
                    departure=SimpleDatetime(date=sf[20], time=sf[8]),
                    arrival=SimpleDatetime(date=sf[21], time=sf[10]),
                    duration=sf[11],
                    plane_type=sf[17],
                ))
            extras = flight[22]
            out.append(Flights(
                type=flight[0],
                price=price,
                airlines=flight[1],
                flights=segments,
                carbon=CarbonEmission(typical_on_route=extras[8],
                                      emission=extras[7]),
            ))
        except Exception:
            # 便の中身が読めない。**その1便だけ捨てる。**
            continue

    return out


def _flight_groups(payload) -> Optional[list]:
    """全ブロックの便を1本に並べる。**読めないブロックは黙って飛ばす。**

    「便が無い」を名乗れるのは最初のブロック（[3]）だけにしてある。
    ここが None なら、取得元が「条件に合う便は無い」と言っている。
    後ろのブロックは**足すだけ**で、無くても・形が違っても異常ではない
    （検索によっては入っていない）。片方が欠けただけで「解析できない」に
    倒すと、2026-09-01 と同じく区間ごと価格を失うことになる。
    """
    if not isinstance(payload, list) or len(payload) <= max(FLIGHT_BLOCKS):
        # 形そのものが違う。**「便が無い」と言ってはいけない。**
        raise ValueError("応答の形が違う（便の入る位置まで届いていない）")

    first, rest = FLIGHT_BLOCKS[0], FLIGHT_BLOCKS[1:]
    head = payload[first]
    if head is None:
        # **該当便が1件も無いページ。** 2026-09-08 に実測で確かめた:
        # SAN→HND を直行便のみで検索すると payload[2..5] が全て None になり、
        # payload[0]・[1] にはメタ情報が入る（＝ページは正しく届いている）。
        #
        # ここを「読めない」に倒していたのが 2026-09-08 まで。ライブラリも
        # 同じで、`payload[3][0]` を無防備に読んで TypeError になっていた。
        # そのため**直行便の無い区間は毎日 fetch_errors に1件積み、しかも
        # 「便が0件だった」証拠（empty_searches）が残らない**状態だった。
        # 運航されていない区間を見つける仕組みが、この経路では働いていない。
        return None
    head = head[0] if isinstance(head, list) and head else None
    if head is None:
        return None                    # 便が無い。**足しに行かない。**

    groups = list(head)
    for index in rest:
        try:
            more = payload[index][0]
        except (TypeError, IndexError, KeyError):
            continue                   # このブロックは無い。それだけのこと
        if isinstance(more, list):
            groups.extend(more)
    return groups


def _identity(k):
    """同じ便を2度数えないための印。読めなければ None（重複判定をしない）。

    **値段だけでは足りない。** 同じ社・同じ経由地で値段まで同じ別便が
    ありうる。区間の空港と日時まで見る。
    """
    try:
        flight = k[0]
        return (k[1][0][1], tuple(flight[1]),
                tuple((s[3], s[6], tuple(s[20] or ()), tuple(s[8] or ()))
                      for s in flight[2]))
    except Exception:
        return None


def search_flights(query) -> list:
    """検索を1回投げて、便の一覧を返す。

    **解析はライブラリに任せない。** `parse_js` は価格の無い便が1件でも
    混ざると区間ごと落とす（`parse_flights` の説明を見よ）。取得だけを
    ライブラリに任せ、読み取りは自前で行う。

    取得と解析をここで1つにしてあるのは、**差し込み口を1か所にする**ため。
    テストはこの関数を差し替えれば足り、HTML を用意しなくてよい。
    """
    return parse_flights(fetch_flights_html(query))


class FastFlightsProvider(PriceProvider):
    name = "fast_flights"

    def __init__(self, settings: Optional[Settings] = None, sleep=time.sleep):
        # sleep を差し替え可能にしてあるのはテストのため。実際に待つと
        # リトライ経路のテストが数十秒かかる。
        self._settings = settings or get_settings()
        self._sleep = sleep
        # 直前の fetch が0件だった理由。**診断用で、扱いは変えない。**
        # collector が fetch の直後に読む。値は Quote 以外の経路で返す必要が
        # あるため属性に置く（PriceProvider の戻り値の形を変えずに済む）。
        #
        #   error_status  応答が errorHasStatus で終わった（取得元が何らかの
        #                 エラーを返した。在庫が無いときにも出る）
        #   empty_page    正しい形のページで、便がゼロ
        #   filtered      便は返ったが、経由回数の検証で全部落ちた
        self.last_empty_reason: Optional[str] = None

    # ------------------------------------------------------------------

    def fetch(self, origin: str, destination: str, departure_date: date,
              stops: int,
              airlines: Optional[Sequence[str]] = None,
              return_date: Optional[date] = None) -> list[Quote]:
        self.last_empty_reason = None
        try:
            results = self._search(origin, destination, departure_date,
                                   stops, airlines, return_date)
        finally:
            # 成功・失敗にかかわらず間隔を空ける。失敗時こそ連打しない。
            self._sleep_politely()

        quotes = []
        for fg in results:
            # (2) セグメント数での検証。max_stops は「以下」なので、stops=1 の
            # 検索に直行便が混ざって返る。フィルタを信じると直行便が
            # 1-stop として二重計上される。
            #
            # **往復のときは往路しか検証できない。** 応答の flights は往路の
            # セグメントだけで、復路は入っていない。max_stops は両区間に
            # 掛けてあるので指定は効いているが、確かめる手立てが無い。
            if len(fg.flights) - 1 != stops:
                continue
            quotes.append(Quote(
                airline=", ".join(fg.airlines),
                stops=stops,
                via_airport=_via_airport(fg),
                price_usd=float(fg.price),
                **_flight_details(fg),
            ))
        if not quotes and self.last_empty_reason is None:
            self.last_empty_reason = "filtered" if results else "empty_page"
        return quotes

    # ------------------------------------------------------------------

    def _sleep_politely(self) -> None:
        self._sleep(random.uniform(*self._settings.request_delay_range))

    def _search(self, origin: str, destination: str, departure_date: date,
                stops: int, airlines: Optional[Sequence[str]],
                return_date: Optional[date] = None) -> list:
        """検索を1回投げる。`return_date` があれば往復。

        **往復でも1リクエスト。** 片道2本に分けて投げるのではない。往復運賃は
        片道の合成では出せないため（NRT-LAX の ANA は片道 $2,164 に対し往復
        $2,226 = +$62。2026-08-19 実測）、往復として引く以外に手が無い。

        返る形は片道と非対称で、**価格は往復の合計だが flights は往路だけ**。
        Quote の docstring を見よ。
        """
        last_error = None
        for attempt in range(1, self._settings.max_retries + 1):
            try:
                legs = [FlightQuery(
                    date=departure_date.isoformat(),
                    from_airport=origin,
                    to_airport=destination,
                    max_stops=stops,
                    airlines=list(airlines) if airlines else None,
                )]
                if return_date is not None:
                    # 復路。経由回数と航空会社の条件は往路と揃える。
                    legs.append(FlightQuery(
                        date=return_date.isoformat(),
                        from_airport=destination,
                        to_airport=origin,
                        max_stops=stops,
                        airlines=list(airlines) if airlines else None,
                    ))
                query = create_query(
                    flights=legs,
                    seat="economy",
                    trip="round-trip" if return_date is not None else "one-way",
                    passengers=Passengers(adults=1),
                    language="en-US",
                    currency="USD",
                )
                return search_flights(query)
            except (FlightsNotFound, TypeError) as exc:
                # **0件の理由を残す（2026-10-12）。扱いは変えない。**
                # FlightsNotFound は parse_flights が errorHasStatus を見たときに
                # だけ投げる。収集中にだけ0件が増える現象（10/1〜）が、取得元の
                # エラーなのか本当に空のページなのかを、翌朝の記録で見分ける。
                self.last_empty_reason = ("error_status"
                                          if isinstance(exc, FlightsNotFound)
                                          else "empty_page")
                # (1) 該当便なし。**エラーではない。**
                #
                # fast_flights は「便が無い」を2通りで知らせてくる。
                #   - FlightsNotFound … 明示的な例外
                #   - bare TypeError  … パース結果が空のときに落ちる経路
                # どちらもフィルタ条件に合う便が存在しないという意味なので、
                # 即座に「なし」として返す。
                #
                # FlightsNotFound を一般の失敗として扱っていた時期があり、
                # 250日先の SAN-HND のように在庫が出ていない路線で3回ずつ
                # リトライしたうえ fetch_errors に積んでいた。本当の障害が
                # 埋もれるので、明示的に分ける。
                #
                # リトライせず即返すのも意図的。「便が無い」は待っても増えず、
                # 直行便の無い路線ごとに無駄なリクエストとバックオフが
                # 積み上がる（500ルート規模では数十分の差）。
                return []
            except Exception as exc:
                last_error = exc
                if attempt < self._settings.max_retries:
                    # (3) 指数バックオフ
                    self._sleep(self._settings.retry_backoff_base_seconds * attempt)

        detail = "".join(traceback.format_exception_only(last_error)).strip()
        leg = (f"{origin}->{destination} {departure_date.isoformat()}"
               + (f" 復路{return_date.isoformat()}" if return_date else ""))
        raise ProviderError(f"{leg} stops={stops}: {detail}") from last_error

    # ------------------------------------------------------------------
    # 価格履歴（補助機能。失敗しても収集を止めない）
    # ------------------------------------------------------------------

    def fetch_history(self, origin: str, destination: str,
                      departure_date: date,
                      return_date: Optional[date] = None) -> list[HistoryPoint]:
        """Google Flights が応答に載せている過去約60日ぶんの最安値を返す。

        **fast_flights が読んでいない領域から取り出す。** ライブラリは
        `payload[3]`（便）と `payload[7]`（メタ）しか使わないが、`payload[5]`
        に価格の判定材料が入っており、その `[10][0]` が
        `[[エポックミリ秒, 価格], ...]` の時系列になっている。

        経由回数で絞らないのは、`snapshots` から引く「ルート最安」と意味を
        揃えるため。直行便に絞ると別の系列になる。

        **往復でも取れる**（2026-08-19 実測、62点）。往復の価格曲線を、
        収集を待たずに検証できるということ。
        """
        try:
            legs = [FlightQuery(
                date=departure_date.isoformat(),
                from_airport=origin,
                to_airport=destination,
            )]
            if return_date is not None:
                legs.append(FlightQuery(
                    date=return_date.isoformat(),
                    from_airport=destination,
                    to_airport=origin,
                ))
            query = create_query(
                flights=legs,
                seat="economy",
                trip="round-trip" if return_date is not None else "one-way",
                passengers=Passengers(adults=1),
                language="en-US",
                currency="USD",
            )
            html = fetch_flights_html(query)
        except Exception:
            # **添字も HTML の形も Google 側の都合で変わる。** 履歴が取れない
            # ことは障害ではないので、ここで握り潰して空を返す。
            return []
        finally:
            self._sleep_politely()

        return parse_history(html)
