"""ダッシュボードの HTML を組み立てる（アプリから呼ぶ薄い層）。

**生成の作法は dashboard/build_dashboard.py 1か所に置く。** ここはそれを
読み込んで呼ぶだけ。二重に持つと、片方だけ直したときに気づけない。
"""
import json
import pathlib
import sys

_DASH = pathlib.Path(__file__).resolve().parent.parent / "dashboard"


def render_dashboard(ops: dict, lang: str) -> str:
    """取得口の生データから、指定言語の HTML を返す。"""
    if str(_DASH) not in sys.path:
        sys.path.insert(0, str(_DASH))
    import build_dashboard as bd

    payload = bd.to_payload(ops)
    bd.PAYLOAD = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # **文言の数字もここで埋める。** 2026-09-10 に、CLI 側（main）だけに
    # 埋める処理を書いて本番のこの経路を通し忘れ、S3 に配ったダッシュボードに
    # 「{days}日ぶん」と波括弧のまま出た。埋める作法は build_dashboard 側の
    # 1関数に置き、両方の経路がそれを呼ぶ。
    bd.fill_strings(payload)
    return bd.build(lang)
