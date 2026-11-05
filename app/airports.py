"""空港マスタ。**空港の定義はこのファイル越しにしか参照しない。**

`static/airports.json` を読む。同じファイルをブラウザも取得して入力補完に
使う — **補完と検証が別々のリストを見ていると、画面では選べるのにサーバが
拒否する（またはその逆）という食い違いが必ず起きる。**

補完だけでは保証にならない。利用者は候補を無視して直接入力できるし、API を
直接叩くこともできる。したがって拒否はサーバ側で行い、補完は入力を楽にする
ためだけのものと位置づける。

データの生成は `scripts/build_airports.py`（開発時のみ実行）。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

DATA_PATH = Path(__file__).resolve().parent.parent / "static" / "airports.json"


@lru_cache(maxsize=1)
def _data() -> dict:
    """マスタを1度だけ読む。

    **ファイルが無い・壊れている場合は空で返さず、例外にする。** 空で返すと
    全ての登録が「未知の空港」で拒否され、原因の分からない全面停止になる。
    起動時に落ちるほうが気づける。
    """
    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    airports = {a[0]: {"code": a[0], "name": a[1], "city": a[2],
                       "country": a[3], "size": a[4], "aliases": a[5]}
                for a in raw["airports"]}
    if len(airports) < 3000:
        raise RuntimeError(
            f"空港マスタが不完全です ({len(airports)} 件): {DATA_PATH}")
    return {"airports": airports, "metros": raw.get("metros", {}),
            "generated": raw.get("generated")}


def exists(code: str) -> bool:
    """定期便のある空港として登録されているか。"""
    return code in _data()["airports"]


def get(code: str) -> Optional[dict]:
    return _data()["airports"].get(code)


def label(code: str) -> str:
    """エラーメッセージなどに出す短い表記。未知ならコードそのもの。"""
    a = get(code)
    return f"{code} ({a['city']}, {a['country']})" if a else code


def metro_members(metro_code: str) -> list[str]:
    """都市コード（TYO, OSA, NYC …）に属する空港。無ければ空。"""
    return _data()["metros"].get(metro_code, [])


def count() -> int:
    return len(_data()["airports"])


def generated_on() -> Optional[str]:
    return _data()["generated"]
