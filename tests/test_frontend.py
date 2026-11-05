"""フロントエンドの JS が「実行できる」ことを確かめる。

**なぜ Python のテスト一式に混ぜるか。** 別立てにすると誰も走らせない。
pytest から呼べば、既存の手順（`python -m pytest`）のまま守られる。

ここが見ているのは見た目ではなく、**識別子の抜け**。2026-08-15 に
帯のプラグインを差し替えたとき、置換範囲を広く取りすぎて keyOf と
byDate を巻き添えで消し、本番の価格トレンドが空になった。
`node --check` は通っていた — 未定義の識別子は構文エラーではないため。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "static" / "js"

# node が無い環境（CI の一部）ではスキップする。**失敗にはしない** —
# ここで落とすと、Python だけ触りたい人が前に進めなくなる。
node = shutil.which("node")
needs_node = pytest.mark.skipif(node is None, reason="node が無い")


@needs_node
@pytest.mark.parametrize("script", sorted(p.name for p in STATIC_JS.glob("*.js")))
def test_every_script_parses(script):
    r = subprocess.run([node, "--check", str(STATIC_JS / script)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@needs_node
def test_removing_a_watch_also_removes_it_server_side():
    """「一覧から外す」が本当に外れるか。

    **2026-08-29 の不具合。** 押しても消えなかった。localStorage からしか
    消しておらず、renderList が一覧トークンを見てサーバ側から取り直し、
    消したものを書き戻していた。**画面が要る種類の不具合**なので、最小の
    DOM を置いて index.js をそのまま走らせる。
    """
    r = subprocess.run([node, str(ROOT / "tests" / "js" / "remove_from_list.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@needs_node
def test_the_chart_renders_without_throwing():
    """**構文検査では防げない壊れ方をここで捕まえる。**

    描画結果ではなく「render が最後まで走るか」を見る。呼び方を変えた
    6通り（便を選択・期限なし・円表示・系列ゼロ・実測ゼロ）を通す。
    """
    r = subprocess.run([node, str(ROOT / "tests" / "js" / "render_chart.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_notification_offer_appears_only_when_we_would_actually_send():
    """通知の誘いが、出るべきときに出て、出てはいけないときに出ないか。

    **場所を変えた変更なので、条件を1つ取り違えると二度と出なくなる。**
    価格トレンドのタブに置いていた10日間、購読は3件から動かなかった
    （125件中84件は作った日以外に開かれない / 2026-09-08 実測）。

    往復・路線が未確定・開始日を過ぎた、のどれかなら notify.py は送らない。
    **送らない通知を誘えば嘘になる**ので、そこも合わせて見る。
    """
    r = subprocess.run([node, str(ROOT / "tests" / "js" / "notify_offer.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_list_page_offer_counts_only_monitors_we_would_notify():
    """一覧の上に出る誘いが、送れるモニターだけを数えているか。

    往復・路線が未確定・値上がり開始日を過ぎたものには notify.py が
    通知を送らない。**送らない通知を誘えば嘘になる。**
    断りがモニター画面と共通の鍵に残ることも合わせて見る。
    """
    r = subprocess.run([node, str(ROOT / "tests" / "js" / "notify_banner.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_message_has_both_languages():
    """片方だけだと、言語を切り替えたときにキー名が生で出る。"""
    src = (STATIC_JS / "i18n.js").read_text(encoding="utf-8")
    missing = []
    for m in re.finditer(r"^\s*'([\w.]+)':\s*\{", src, re.M):
        i = m.end() - 1
        depth = 0
        for j in range(i, len(src)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
        body = src[i:j + 1]
        if ("ja:" in body) != ("en:" in body):
            missing.append(m.group(1))
    assert not missing, f"日英が片方だけ: {missing}"


def test_no_message_key_is_used_without_being_defined():
    """`I18N.t('...')` の綴り間違いは画面にキー名を出す。"""
    i18n = (STATIC_JS / "i18n.js").read_text(encoding="utf-8")
    defined = set(re.findall(r"^\s*'([\w.]+)':\s*\{", i18n, re.M))

    used = {}
    for path in STATIC_JS.glob("*.js"):
        for m in re.finditer(r"I18N\.t\(\s*'([\w.]+)'", path.read_text(encoding="utf-8")):
            used.setdefault(m.group(1), path.name)

    unknown = {k: v for k, v in used.items() if k not in defined}
    assert not unknown, f"未定義のキー: {unknown}"


def test_placeholders_match_the_arguments_passed():
    """文言が `{n}` を求めているのに渡していなければ、生で表示される。

    日英でプレースホルダが食い違う場合も拾う（英語だけ `{d}` が残る等）。
    """
    i18n = (STATIC_JS / "i18n.js").read_text(encoding="utf-8")

    holes = {}
    for m in re.finditer(r"^\s*'([\w.]+)':\s*\{", i18n, re.M):
        i = m.end() - 1
        depth = 0
        for j in range(i, len(i18n)):
            if i18n[j] == "{":
                depth += 1
            elif i18n[j] == "}":
                depth -= 1
                if depth == 0:
                    break
        body = i18n[i:j + 1]
        ja_part, _, en_part = body.partition("en:")
        holes[m.group(1)] = (set(re.findall(r"\{(\w+)\}", ja_part)),
                             set(re.findall(r"\{(\w+)\}", en_part or ja_part)))

    problems = []
    for path in STATIC_JS.glob("*.js"):
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"I18N\.t\(\s*'([\w.]+)'\s*(?:,\s*\{([^{}]*)\})?\s*\)", src):
            key, args = m.group(1), m.group(2) or ""
            passed = set(re.findall(r"(\w+)\s*:", args))
            need_ja, need_en = holes.get(key, (set(), set()))
            if need_ja != need_en:
                problems.append(f"{key}: 日英でプレースホルダが違う "
                                f"ja={sorted(need_ja)} en={sorted(need_en)}")
            if (need_ja | need_en) - passed:
                problems.append(f"{path.name} の {key}: "
                                f"{sorted((need_ja | need_en) - passed)} を渡していない")
    assert not problems, problems


def test_the_changelog_is_valid_and_bilingual():
    """更新履歴は利用者に直接出る。**壊れると画面ごと出なくなる。**"""
    src = (STATIC_JS / "changelog.js").read_text(encoding="utf-8")
    body = src[src.index("const CHANGELOG = ") + len("const CHANGELOG = "):]
    body = body[:body.rindex("]") + 1]
    # JSON にできる形か（末尾カンマ・引用符の崩れをここで拾う）。
    entries = json.loads(re.sub(r",(\s*[\]}])", r"\1",
                                re.sub(r"^(\s*)(\w+):", r'\1"\2":', body, flags=re.M)
                                .replace("'", '"')))
    assert entries, "更新履歴が空"
    for day in entries:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day["date"]), day["date"]
        for item in day["items"]:
            assert item.get("ja") and item.get("en"), f"{day['date']}: 日英が揃っていない"


def test_no_message_contains_markdown():
    """文言は `textContent` に入る。**装飾記法はそのまま文字として出る。**

    2026-08-15、設定タブの「近くの空港」の説明に `**既定ではオフ**` が
    入ったまま公開されていた（利用者にはアスタリスクが見えていた）。
    コードコメントでは強調に使う書き方なので、文言へ紛れ込みやすい。
    """
    src = (STATIC_JS / "i18n.js").read_text(encoding="utf-8")
    offenders = []
    for m in re.finditer(r"^\s*(ja|en):\s*'((?:[^'\\]|\\.)*)'", src, re.M):
        text = m.group(2)
        for mark in ("**", "__", "`"):
            if mark in text:
                offenders.append(f"{m.group(1)}: …{text[:50]}… に {mark}")
    assert not offenders, offenders


# --- PWA（課題4・段階D）------------------------------------------------
#
# **この試験が生まれた理由。** sw.js が /static/icon-192.png を指したまま
# 画像を置き忘れ、本番で通知が有効になったあと2日間、アイコン無しで配信
# されていた。node --check も既存の試験も、参照先の実在は見ていない。

STATIC = ROOT / "static"


def _referenced_images() -> set[str]:
    """sw.js・manifest.json・HTML が参照している /static の画像を集める。"""
    found: set[str] = set()
    for path in (STATIC / "sw.js", STATIC / "manifest.json",
                 STATIC / "index.html", STATIC / "watch.html"):
        found |= set(re.findall(r"/static/([\w.-]+\.png)", path.read_text(encoding="utf-8")))
    return found


def test_every_referenced_image_exists():
    missing = sorted(n for n in _referenced_images() if not (STATIC / n).is_file())
    assert not missing, f"参照されているのに無い画像: {missing}"


def test_the_manifest_covers_the_whole_site():
    """**scope を書き忘れると /static/ に閉じる。**

    manifest.json は /static 配下に置いてあるので、scope を省くと既定が
    /static/ になり、モニター画面（/w/{token}）が単独窓の外に出る。
    iOS ではそこが通知の可否に直結する。
    """
    m = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))
    assert m["scope"] == "/"
    assert m["start_url"] == "/"
    # iOS で Web Push が使えるのは単独窓のときだけ。
    assert m["display"] == "standalone"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes, sizes
    # Android の適応アイコンは円に切る。切られてよい版が要る。
    assert any(i.get("purpose") == "maskable" for i in m["icons"])


def test_the_badge_is_not_the_colour_icon():
    """**badge に色付きアイコンを渡すと四角い塊になる。**

    Android は badge をアルファだけ見る白黒のマスクとして扱う。
    """
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    badge = re.search(r"badge:\s*'/static/([\w.-]+)'", sw)
    assert badge, "sw.js に badge の指定が無い"
    assert badge.group(1) != "icon-192.png", "badge が色付きアイコンの使い回しになっている"


@pytest.mark.parametrize("page", ["index.html", "watch.html"])
def test_the_pages_can_be_installed(page):
    """ホーム画面に追加されたとき単独窓で開くための宣言。

    **iOS は manifest の display を見ない。** apple- 系のタグが要る。
    """
    html = (STATIC / page).read_text(encoding="utf-8")
    assert 'rel="manifest"' in html
    assert 'name="apple-mobile-web-app-capable"' in html
    assert 'rel="apple-touch-icon"' in html


def test_the_permission_request_is_not_behind_an_await(static_dir=None):
    """**Safari は許可要求を利用者の操作と同じ処理の中でしか受け付けない。**

    subscribe() の中で requestPermission より前に await を置くと、操作との
    結び付きが切れて許可ダイアログが出ないまま失敗する。iPad で
    「チェックが付かない」として報告された（2026-08-21）。

    公開鍵は prime() で先に取っておく約束なので、subscribe() の本体に
    fetch や config() が残っていないことを見る。
    """
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent
              / "static" / "js" / "push.js").read_text(encoding="utf-8")

    start = source.index("async function subscribe(")
    body = source[start:source.index("requestPermission", start)]
    assert "await fetch(" not in body, "requestPermission の前に fetch がある"
    assert "await config(" not in body, "requestPermission の前に config() がある"


def test_ios_browsers_are_not_told_they_are_unsupported():
    """**iPhone・iPad を「非対応」で片付けない。**

    タブでは PushManager が無いだけで、ホーム画面に追加すれば使える。
    iOS の Chrome から試した利用者が「チェックが付かない」と報告した
    （2026-08-21）。Chrome も中身は WebKit なので同じ制約を受けるが、
    追加の手順は Safari のものを案内する必要がある。
    """
    from pathlib import Path
    js = Path(__file__).resolve().parent.parent / "static" / "js"
    push = (js / "push.js").read_text(encoding="utf-8")
    watch = (js / "watch.js").read_text(encoding="utf-8")
    i18n = (js / "i18n.js").read_text(encoding="utf-8")

    # iPad は自分を Mac と名乗るので、タッチ点数まで見ていること。
    assert "maxTouchPoints" in push
    # iOS の別ブラウザを見分けていること。
    assert "CriOS" in push
    # 3通りの案内が出し分けられていること。
    for key in ("push.iosOtherBrowser", "push.ios", "push.unsupported"):
        assert f"'{key}'" in i18n, f"{key} の文言が無い"
        assert key in watch, f"{key} が画面で使われていない"
