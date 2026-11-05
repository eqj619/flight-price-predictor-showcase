"""VAPID 鍵ペアを1組つくる。**サーバで1回だけ実行する。**

    python -m scripts.gen_vapid_keys

出力を /etc/fpp/env に書き足す。公開鍵はブラウザに配るので秘密ではないが、
**秘密鍵が漏れると第三者が我々を名乗って通知を送れる。**

**鍵を作り直すと既存の購読がすべて無効になる**（購読はブラウザ側で公開鍵に
紐づく）。利用者に許可を取り直させることになるので、失くさないこと。

py_vapid は base64url の文字列を直接くれないので、生バイト列から自分で作る。
公開鍵は非圧縮点（65バイト、先頭 0x04）、秘密鍵はスカラー32バイト。
ブラウザの applicationServerKey と Authorization ヘッダの k= に、この形の
まま入る。
"""
import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01


def _b64(raw: bytes) -> str:
    """base64url、パディング無し。Web Push はどこでもこの形。"""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def generate() -> tuple[str, str]:
    """(公開鍵, 秘密鍵) を base64url で返す。"""
    vapid = Vapid01()
    vapid.generate_keys()
    public = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    return _b64(public), _b64(private)


def main() -> int:
    public, private = generate()
    print("# /etc/fpp/env に書き足す。**秘密鍵は他所に出さない。**")
    print(f"VAPID_PUBLIC_KEY={public}")
    print(f"VAPID_PRIVATE_KEY={private}")
    print("# 送信元の連絡先。プッシュサービスが問題時に参照する（RFC 8292）。")
    print("VAPID_SUBJECT=mailto:admin@suosolutions.com")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
