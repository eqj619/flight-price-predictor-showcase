"""価格曲線のパラメータ。

既存 flight-price-monitor の simulator.py はこれらをモジュール定数として
持っていた。本アプリでは Phase 3.5（完了した系列から実測で推定した値に
差し替える）を見据えて、データとして外に出す。

差し替え方は2通り:

- `CURVE_PARAMS_PATH` に JSON を置く（実測から推定した値を書き出す想定）
- テストや実験では `CurveParams(...)` を直接組み立てて渡す

値の出所は既存アプリの東京 <-> 米西海岸市場の観測に基づく初期値で、
統計的に推定されたものではない。**較正（observed / shape）がスケール項を
吸収するため、曲線の「高さ」より「形」が重要**である点に注意。
"""
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional


def _default_airline_factors() -> dict[str, float]:
    # 参照運賃に対する各社の価格ポジション。
    #
    # 実測で返る航空会社名（"United", "American", "ZIPAIR Tokyo"）とは
    # 一致しないものがある。一致しなくても実害は小さい: この係数は
    # check_date に依存しない定数なので、較正のスケール項に丸ごと
    # 吸収される。名前の揺れを追いかける表を育てるより、較正に任せる。
    return {
        "ANA": 1.30,
        "JAL": 1.28,
        "United Airlines": 1.05,
        "Delta Air Lines": 1.08,
        "American Airlines": 1.10,
        "Air Canada": 1.00,
        "Korean Air": 0.95,
        "ZIPAIR": 0.55,
    }


def _default_seasonality() -> dict[int, float]:
    return {
        1: 0.85, 2: 0.85,          # 閑散期
        3: 1.15, 4: 1.15,          # 桜シーズン
        5: 1.00, 6: 1.00,
        7: 1.25, 8: 1.25,          # 夏休み
        9: 0.85, 10: 0.85,         # 閑散期
        11: 1.00,
        12: 1.25,                  # 年末年始
    }


def _default_day_of_week() -> dict[int, float]:
    # Mon=0 ... Sun=6
    return {0: 1.00, 1: 0.90, 2: 0.90, 3: 1.00, 4: 1.15, 5: 1.00, 6: 1.15}


# ── 方向別の上書き（2026-10-14）────────────────────────────────
#
# **底値の時期は、日本発と米国発で違う。** 出発日を迎えた系列で「出発の
# ◯日前に買ったら、その日に買える最安が底値より何%高かったか」を数えた
# （2026-08-23〜10-11 出発、区間最安。docs/issues.md 課題16）。
#
#     買った日   日本発→米西海岸(36系列)        米西海岸発→日本(31系列)
#                中央値 / 底値の5%以内           中央値 / 5%以内
#     75日前     +8.2% / 41%                     +28.6% / 10%
#     65日前     +7.5% / 36%                     +21.9% /  6%
#     52日前     +12.5% /  6%   ← 旧期限        +27.4% / 16%
#     45日前     +29.0% /  0%                    +26.4% / 16%
#
# 日本発は 65〜75日前が最良で、52日前はもう上がり始めた後だった。米国発は
# どの日もほぼ同じで、期限を動かしても得にならない。**全方向に1本の谷を
# 当てていたのが、助言の外れの大きな原因。**
#
# **動かすのは日本発→米西海岸だけ。** 米国発は1シーズン（閑散期）しか
# 無く、年末年始の結果を待つ（利用者の判断、2026-10-14）。対象の空港は
# 実測があった範囲に限る。根拠の無い路線に広げない。
#
# **予測線には効かない。** 谷の深さは 0.5% なので、過去データで当て直すと
# 価格の予測精度は全区間で現行と同値だった（scripts/replay_curve.py、
# 1,640系列）。変わるのは「値上がりが始まる日」と判定の基準日。
JAPAN_AIRPORTS = frozenset({"NRT", "HND", "KIX", "ITM", "NGO", "FUK", "CTS",
                            "OKA", "KMJ", "OIT", "NGS", "USM"})
US_WEST_AIRPORTS = frozenset({"SAN", "LAX", "SFO", "SEA", "PDX"})
JP_TO_US_WEST = "JP->US-West"


def route_direction(origin: str, destination: str) -> Optional[str]:
    """方向別の上書きの鍵。該当しなければ None。"""
    if origin in JAPAN_AIRPORTS and destination in US_WEST_AIRPORTS:
        return JP_TO_US_WEST
    return None


def _default_direction_overrides() -> dict[str, dict]:
    return {JP_TO_US_WEST: {"sweet_spot_start_days": 65,
                            "sweet_spot_end_days": 75}}


@dataclass(frozen=True)
class CurveParams:
    """価格曲線を決めるパラメータ一式。"""

    # --- 水準（較正で吸収されるので精度は重要でない）---
    base_fare_usd: float = 620.0
    airline_factors: dict[str, float] = field(default_factory=_default_airline_factors)
    default_airline_factor: float = 1.0
    nonstop_factor: float = 1.25
    connecting_factor: float = 1.00

    # --- 出発日で決まる係数（check_date に依存しない = 較正で吸収される）---
    seasonality: dict[int, float] = field(default_factory=_default_seasonality)
    day_of_week: dict[int, float] = field(default_factory=_default_day_of_week)

    # --- 出発までの日数で決まる係数 ---
    # **ここだけが check_date とともに動く。** 較正後の予測はほぼこの曲線の
    # 形だけで決まる。
    #
    # 2026-08-15、**実測から推定した値に差し替えた**（Phase 3.5）。
    # 212系列 / 13,004点。scripts/fit_curve.py で再現できる。
    # 経緯と検定は docs/price-curve-2026-08-15.md。
    #
    # **旧値の「45〜75日前が底」は、窓が広すぎた。** 実測では 45〜75日前と
    # それ以前(76〜200日前)に差が出ない（32/66系列, 符号検定 z=-0.2）。
    # 窓の中に安い部分と安くない部分が混ざり、平均すると消えていた。
    #
    # **谷そのものは実在する。** 50〜59日前に絞ると -4.2%（29/40系列,
    # z=+2.8）で有意。旧値の -15%（0.85）は深すぎたが、ゼロでもない。
    # ここで当てはめた 0.995（-0.5%）は、この式で全区間を同時に説明しようと
    # した結果、谷が浅く出たもの。**区間比較の -4.2% のほうが直接的な測定**で、
    # 両者の食い違いは式の表現力の限界。docs/price-curve-2026-08-15.md 参照。
    #
    # **効くのは遅らせないこと。** 出発30日前を過ぎると +16.7%
    # （35/41系列, z=+4.5）。谷の深さの4倍以上。
    sweet_spot_start_days: int = 52
    sweet_spot_end_days: int = 59
    sweet_spot_factor: float = 0.995
    # far_taper は「底値帯より前は徐々に高くなる」という想定だったが、
    # **実測はそうなっていない**（60〜160日前は平坦）。負値も探索させたが
    # 0 が選ばれた。165日前より前は実測 0.95 とむしろ安いものの、
    # 「底値帯から直線」というこの式では中間の平坦部を壊さずに表現できない。
    # **式の限界。** 早期購入が不利でないことだけは押さえておく。
    far_taper_days: int = 20
    far_taper_amount: float = 0.0

    # --- 直前の上昇（2026-08-24 に大幅に下げた）---
    #
    # **これらは「ルート最安」の推移から取った値を、便ごとの予測に当てていた。**
    # 別の量の形を別の量に当てていたことになる。
    #
    #     ルート最安  安い便が売り切れれば上がる（日次 5.27%）
    #     便ごと      その便の値段だけ（日次 1.61%）
    #
    # 実測との突き合わせで、出発14日以内の予測が **+16.9% 高い**（絶対誤差
    # 17.4%、信頼帯に入るのは 49%）と分かった
    # （docs/prediction-accuracy-2026-08-24.md）。
    #
    # 便ごとの系列 3,952本を二元固定効果で復元すると、直前もほぼ平坦だった。
    #
    #     出発30日前 0.918   18日前 0.850   10日前 0.909   5日前 1.058
    #
    # 旧値（30日前 1.095 → 当日 2.5）とは桁違いに緩い。scripts/replay_curve.py
    # で過去データに当て直した結果:
    #
    #     旧              0-14日前  絶対誤差 17.4%  偏り +16.9%
    #     peak 1.30                        6.1%       +4.2%
    #     peak 1.10（採用）                2.8%       +1.7%
    #     peak 1.00                        0.5%       -0.2%
    #
    # **完全に平坦（1.00）が最良だが、採らない。** 出発14日以内の観測は
    # 出発日が2〜4種類しかなく、日付ごとの効果と分けられていない。
    # 実測の形（1〜6日前で約1.05）に合う 1.10 にして、「上がらない」と
    # 決めつけない側に倒す。**系列が増えたら測り直すこと。**
    ramp_end_days: int = 18           # ここまでは直線的に上昇
    ramp_top_factor: float = 1.0
    last_minute_start_factor: float = 1.0   # 18日前
    last_minute_peak_factor: float = 1.10   # 当日

    # --- 方向別の上書き（上の「方向別の上書き」を参照）---
    direction_overrides: dict[str, dict] = field(
        default_factory=_default_direction_overrides)

    # --- ゆらぎ（シミュレーション用スナップショットのみで使う）---
    jitter_min: float = 0.97
    jitter_max: float = 1.03


DEFAULT_PARAMS = CurveParams()

# JSON のキーは必ず文字列になるので、int キーの辞書は読み込み時に戻す。
_INT_KEYED_FIELDS = ("seasonality", "day_of_week")


def load_params(path: Optional[Path] = None) -> CurveParams:
    """JSON があればそれで既定値を上書きし、無ければ既定値を返す。

    部分的な上書きを許す（推定できた項目だけ書いた JSON でよい）。
    """
    if path is None:
        from app.config import get_settings
        path = get_settings().curve_params_path

    if path is None or not Path(path).exists():
        return DEFAULT_PARAMS

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    known = {f for f in DEFAULT_PARAMS.__dataclass_fields__}
    overrides = {}
    for key, value in raw.items():
        if key not in known:
            continue    # 未知のキーは黙って無視する（前方互換）
        if key in _INT_KEYED_FIELDS:
            value = {int(k): float(v) for k, v in value.items()}
        overrides[key] = value
    return replace(DEFAULT_PARAMS, **overrides)


def route_params(origin: str, destination: str,
                 params: Optional[CurveParams] = None) -> CurveParams:
    """その区間に使うパラメータ。方向別の上書きがあれば当てる。

    **区間が分かる入口（build_trend / recommend / 通知）で1回だけ呼ぶ。**
    下流の関数は受け取った params をそのまま使う。
    """
    params = params or load_params()
    override = params.direction_overrides.get(route_direction(origin, destination) or "")
    if not override:
        return params
    known = set(params.__dataclass_fields__) - {"direction_overrides"}
    return replace(params, **{k: v for k, v in override.items() if k in known})
