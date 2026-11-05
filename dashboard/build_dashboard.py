#!/usr/bin/env python3
"""運用ダッシュボード（日英）を1つの構造から生成する。

**片方だけ古くなるのを防ぐ。** 同じ内容を2つの HTML で手作業維持すると、
データを更新したときに必ず片方が取り残される。文言だけを STRINGS に分け、
構造・配色・描画コードは共有する（scripts/build_route_map.py と同じ作法）。

毎朝クラウド側のルーティンが、本番の /api/ops/dashboard.json を取得して
これを走らせる。ルーティンは本番 DB にも S3 にも触れないので、入力は
その JSON 1つだけで完結させてある。

    python3 dashboard/build_dashboard.py --data ops.json --out-dir .
      → fpp-dashboard.html      (ja)
      → fpp-dashboard.en.html   (en)
"""
import argparse
import copy
import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
CSS = (HERE / "dashboard.css").read_text(encoding="utf-8")
JS_SRC = (HERE / "dashboard.js").read_text(encoding="utf-8")

# ── 描画コードから日本語を追い出す ──────────────────────────────
# 逐語置換。正規表現だと日本語のコメントまで巻き込む。
JS_PATCHES = [
    # 状態帯
    ("""  const cells = [
    ['監視中のモニター', m.total, '件', `作成者 ${m.creators} 人`],
    ['稼働ルート', m.routes, '/1000', '54% は調査用'],
    ['実測レコード', m.recs.toLocaleString(), '', '観測 23 日'],
    ['完了した系列', m.done, '本', '出発日を過ぎたもの'],
    ['通知の購読', m.subs, '件', `${m.total} 件中 ${(m.subs / m.total * 100).toFixed(0)}%`],
  ];""",
     """  const cells = [
    [S.k_total, m.total, S.u_item, S.s_total.replace('{n}', m.creators)],
    [S.k_routes, m.routes, '/' + (m.route_cap || 1000), S.s_routes],
    [S.k_recs, m.recs.toLocaleString(), '', S.s_recs],
    [S.k_done, m.done, S.u_series, S.s_done],
    [S.k_subs, m.subs, S.u_item,
      S.s_subs.replace('{n}', m.total).replace('{p}', (m.subs / m.total * 100).toFixed(0))],
  ];"""),
    # 小さな折れ線
    ("'aria-label': `${opts.label} の推移` });",
     "'aria-label': `${opts.label} — ${S.a_trend}` });"),
    ("`<span class=\"delta ${cls}\">前日比 ${sign}",
     "`<span class=\"delta ${cls}\">${S.vs_prev} ${sign}"),
    ("""    { key: 'total',    label: 'モニター総数',   note: '登録されている監視',
      tone: 's1', zero: true, upIsBad: false },
    { key: 'new',      label: '新規登録 / 日',  note: '24時間の作成数',
      tone: 's1', zero: true, upIsBad: false },
    { key: 'ret7',     label: '7日以内の再訪率', note: '一度でも開き直された割合',
      tone: 's2', zero: true, fmt: pct, upIsBad: false },
    { key: 'routes',   label: '稼働ルート',     note: '毎日価格を取りに行く本数',
      tone: 's2', zero: true, upIsBad: false },""",
     """    { key: 'total',  label: S.c1_t, note: S.c1_n, tone: 's1', zero: true, upIsBad: false },
    { key: 'new',    label: S.c2_t, note: S.c2_n, tone: 's1', zero: true, upIsBad: false },
    { key: 'ret7',   label: S.c3_t, note: S.c3_n, tone: 's2', zero: true,
      fmt: pct, upIsBad: false,
      // **この1枚だけ解釈が要る。** 他の3つは数えているだけ。
      explain: (pts) => {
        const now = pts[pts.length - 1].v, first = pts[0].v;
        return S.c3_x.replace('{now}', now.toFixed(0))
                     .replace('{gone}', (100 - now).toFixed(0))
                     .replace('{first}', first.toFixed(0));
      } },
    { key: 'routes', label: S.c4_t, note: S.c4_n, tone: 's2', zero: true, upIsBad: false },"""),
    # 精度
    ("t.textContent = '曲線を引き直し'; svg.appendChild(t);",
     "t.textContent = S.refit; svg.appendChild(t);"),
    ("""    { label: '誤差と偏り（出発30日以内）',
      note: '真ん中の1件がどれだけ外れるか（中央値）',
      series: [
        { key: 'aerr',  name: '絶対誤差',       tone: 's3' },
        { key: 'abias', name: '偏り（＋は高すぎ）', tone: 's1' },
      ] },
    // **裾は別のグラフにする。** P90 は中央値の6〜7倍あるので、同じ
    // 縦軸に載せると中央値の推移が下端に潰れて読めなくなる（実測。
    // 8.4% の線が 55.5% と並ぶと、8月24日の引き直しの効果が見えない）。
    { label: '外れ方の裾（出発30日以内）',
      note: '9割の予測が収まるレート（P90）',
      series: [
        { key: 'aerrp', name: '絶対誤差 P90', tone: 's3', dash: true },
        { key: 'abiasp', name: '偏り P90',    tone: 's1', dash: true },
      ] },
    { label: '信頼帯に入った割合',
      note: '示した価格帯の中に実測が収まった割合',
      series: [
        { key: 'aband', name: '出発30日以内', tone: 's3' },
        { key: 'oband', name: '全体',         tone: 's2' },
      ] },""",
     """    { label: S.a1_t, note: S.a1_n,
      series: [{ key: 'aerr', name: S.a1_s1, tone: 's3' },
               { key: 'abias', name: S.a1_s2, tone: 's1' }],
      // **例え話の金額は当日の値から作る。** 固定文にすると、
      // 数字が動いた日に文章のほうが嘘になる。
      explain: (rows) => {
        const r = rows[rows.length - 1];
        const err = Math.round(r.aerr * 10);
        const bias = 1000 + Math.round(r.abias * 10);
        return S.a1_x
          .replace('{e}', err)
          .replace('{b}', bias.toLocaleString())
          .replace('{d}', r.abias >= 0 ? S.too_high : S.too_low);
      } },
    { label: S.a3_t, note: S.a3_n,
      series: [{ key: 'aerrp', name: S.a1_s3, tone: 's3', dash: true },
               { key: 'abiasp', name: S.a1_s4, tone: 's1', dash: true }],
      explain: (rows) => {
        // **裾はまだ測りはじめたばかり。** 無い日は無いと言う。
        const withTail = rows.filter((r) => r.aerrp !== null
                                         && r.aerrp !== undefined);
        if (!withTail.length) return S.a3_none;
        const r = withTail[withTail.length - 1];
        return S.a3_x
          .replace('{p}', r.aerrp.toFixed(1))
          .replace('{w}', Math.round(r.aerrp * 10).toLocaleString())
          .replace('{m}', r.aerr.toFixed(1))
          .replace('{v}', Math.round(r.aerr * 10).toLocaleString());
      } },
    { label: S.a2_t, note: S.a2_n,
      series: [{ key: 'aband', name: S.a2_s1, tone: 's3' },
               { key: 'oband', name: S.a2_s2, tone: 's2' }],
      explain: (rows) => {
        const r = rows[rows.length - 1];
        return S.a2_x.replace('{p}', r.aband.toFixed(0))
                     .replace('{q}', (100 - r.aband).toFixed(0));
      } },"""),
    # 下落トップ10
    ("'aria-label': '開始時からの最大下落率 上位10路線' });",
     "'aria-label': S.d_aria });"),
    ("sub.textContent = r.dep.slice(5) + ' 発'; svg.appendChild(sub);",
     "sub.textContent = S.dep_on.replace('{d}', r.dep.slice(5)); svg.appendChild(sub);"),
    ("""        `<b>${r.leg}</b> ${r.dep} 発<br>` +
        `<span class="k">開始</span><b>$${r.start.toLocaleString()}</b>` +
        ` <span class="k">→ 最安</span><b>$${r.low.toLocaleString()}</b><br>` +
        `<span class="k">下落</span><b>${r.drop_pct}%</b>` +
        ` <span class="k">最安は出発</span><b>${r.low_dbd}日前</b>`));""",
     """        `<b>${r.leg}</b> ${S.dep_on.replace('{d}', r.dep)}<br>` +
        `<span class="k">${S.t_start}</span><b>$${r.start.toLocaleString()}</b>` +
        ` <span class="k">→ ${S.t_low}</span><b>$${r.low.toLocaleString()}</b><br>` +
        `<span class="k">${S.t_drop}</span><b>${r.drop_pct}%</b>` +
        ` <span class="k">${S.t_when}</span><b>${S.dbd.replace('{n}', r.low_dbd)}</b>`));"""),
    ("""        <td class="num">${r.n}日</td><td class="num">$${r.start.toLocaleString()}</td>""",
     """        <td class="num">${S.days.replace('{n}', r.n)}</td>
        <td class="num">$${r.start.toLocaleString()}</td>"""),
    ("""        <td class="num">出発${r.low_dbd}日前</td>""",
     """        <td class="num">${S.dbd.replace('{n}', r.low_dbd)}</td>"""),
]

STRINGS = {
    "ja": {
        "lang": "ja", "dir": "ltr",
        "title": "Flight Price Predictor 運用ダッシュボード",
        "font": "Zen+Kaku+Gothic+New:wght@400;500;700",
        "family": '"Zen Kaku Gothic New", -apple-system, BlinkMacSystemFont,\n'
                  '            "Hiragino Sans", "Noto Sans JP", sans-serif',
        "eyebrow": "flight-price-predictor · 2026-08-10 → 09-01",
        "h1": "運用ダッシュボード",
        "lede": "日次 KPI {days}日ぶんと、本番データベースの実測から作成。"
                "最初に<strong>買い時の検証</strong>——このアプリの目的である"
                "「底値の時期」を、出発日を迎えた便で答え合わせする。続いて"
                "利用状況と、モニター開始時より価格が下がった路線。"
                "価格1点ごとの予測誤差は、末尾の技術指標に畳んだ。",
        # 状態帯
        "k_total": "監視中のモニター", "k_routes": "稼働ルート",
        "k_recs": "実測レコード", "k_done": "完了した系列", "k_subs": "通知の購読",
        "u_item": "件", "u_series": "本",
        "s_total": "作成者 {n} 人", "s_routes": "{research}% は調査用",
        "s_recs": "観測 {days} 日", "s_done": "出発日を過ぎたもの",
        "s_subs": "{n} 件中 {p}%",
        # 節0 買い時の検証（2026-10-12）
        "h2_buy": "買い時の検証 — 底値はいつ来たか",
        "p_buy": "出発日を迎えた <strong>{bn} 系列</strong>（出発 {bd0}〜{bd1}、"
                 "片道、出発60日前から7日前まで通して観測できたもの）で答え合わせした。"
                 "価格は毎朝の自前の実測だけで、取得元の過去価格は混ぜていない"
                 "（混ぜると出発から遠い日が安く見える）。"
                 "観測の始まりは出発の中央値 {obs}日前。",
        "b_none": "この欄のデータはまだ届いていない（取得口が古い）。",
        "b_axis": "出発までの日数 →",
        "b_range": "出発{a}〜{b}日前", "b_range_open": "出発{a}日以上前",
        "b_buy_on": "出発{d}日前に買った場合",
        "b_dir": "{t} — {dir}",
        "b_split": "<br>以下は方向別。日本発 {jp} 系列、米国西海岸発 {us} 系列"
                   "（どちらでもない路線の {other} 系列は除く）。",
        "b1_cens": "{m}/{bn} 系列しか谷の初めの日から観測できていないので、"
                   "谷より前に底があったかはまだ判断できない。",
        "b1_t": "実際の底値は、出発の何日前だったか",
        "b1_n": "{bn} 系列の内訳（同じ最安が続いたら最後の日）",
        "b1_l1": "系列の数", "b1_l2": "アプリが底値と見ている{lo}〜{hi}日前",
        "b1_k": "底値がここ",
        "b1_x": "<b>読み方。</b>アプリが底値の時期と見ている "
                "<span class=\"eg\">{lo}〜{hi}日前</span> に底が来たのは "
                "<span class=\"eg\">{vp}%</span>（{vn}/{bn}）。それより前が "
                "<span class=\"eg\">{ep}%</span>、後が <span class=\"eg\">{lp}%</span>。"
                "<br><b>{verdict}</b>",
        "b1_early": "底値はアプリの見立てより早く来ることが多い。",
        "b1_late": "底値はアプリの見立てより遅く来ることが多い。",
        "b1_both": "底値は谷の前後に散らばっていて、1つの時期には絞れていない。",
        "b2_t": "出発の◯日前に買ったら、底値より何%高かったか",
        "b2_n": "中央値。{near}%以内なら「底値で買えた」とみなす",
        "b2_l1": "買った日", "b2_l2": "米国発などの期限（{d}日前）",
        "b2_l3": "差が最も小さかった日",
        "b2_k1": "底値との差（中央値）", "b2_k2": "平均",
        "b2_k3": "底値の{near}%以内", "b2_k4": "系列",
        "b2_x": "<b>読み方。</b>全方向をまとめた表。日本発→米西海岸以外の期限、"
                "<span class=\"eg\">出発{d}日前</span> に買うと底値より中央値で "
                "<span class=\"eg\">+{p}%</span>（底値の5%以内で買えたのは "
                "<span class=\"eg\">{w}%</span>）。平均の差が最も小さかったのは "
                "<span class=\"eg\">{best}日前</span> で <span class=\"eg\">+{pb}%</span>。"
                "<span class=\"eg\">{last}日前</span> まで待つと "
                "<span class=\"eg\">+{pl}%</span>。<br>"
                "<b>早すぎても遅すぎても高い。</b>期限の前後数日で差が大きく変わる"
                "なら、期限の置き場所そのものを見直す材料になる。",
        "b7_t": "方向別: ◯日前に買ったら底値より何%高かったか — {dir}",
        "b7_n": "中央値。{s} 系列",
        "b7_none": "<b>まだこの方向の系列がない。</b>",
        "b7_l2": "この方向の期限（{d}日前）",
        "b7_x": "<b>読み方。</b>この方向の期限 <span class=\"eg\">出発{d}日前</span> に"
                "買うと、底値より中央値で <span class=\"eg\">+{p}%</span>（底値の5%以内は "
                "<span class=\"eg\">{w}%</span>）。平均の差が最も小さかったのは "
                "<span class=\"eg\">{best}日前</span>（<span class=\"eg\">+{pb}%</span>）、"
                "<span class=\"eg\">{last}日前</span> まで待つと "
                "<span class=\"eg\">+{pl}%</span>。<br><b>期限と最小の日が離れていれば、"
                "期限を見直す材料になる。</b>",
        "b7_x_nr": "<b>読み方。</b>この方向の期限 <span class=\"eg\">出発{d}日前</span> に"
                   "買った場合は、<b>まだ観測できた系列がない</b>。平均の差が最も小さかったのは "
                   "<span class=\"eg\">{best}日前</span>（<span class=\"eg\">+{pb}%</span>）、"
                   "<span class=\"eg\">{last}日前</span> まで待つと "
                   "<span class=\"eg\">+{pl}%</span>。",
        "dir_JP->US-West": "日本発 → 米国西海岸",
        "dir_US-West->JP": "米国西海岸発 → 日本",
        "b3_t": "アプリの判定は当たったか",
        "b3_n": "今買う／待つ／様子見 を毎朝記録し、出発後に答え合わせ",
        "b3_wait": "<b>まだ答え合わせできる判定がない。</b>判定の記録は "
                   "<span class=\"eg\">{since}</span> から始めた。記録した便が"
                   "出発日を迎えると、ここに当たり外れが出る。",
        "b3_th": ["判定", "件数", "当たり", "その後の最安との差", "当たりの定義"],
        "b3_x": "<b>読み方。</b>{since} からの記録のうち、出発日を迎えた "
                "<span class=\"eg\">{ev}</span> 件。比べる相手は判定した日"
                "<b>より後</b>に買えた最安（過去には戻って買えない）。"
                "出発の前日・当日に出した判定は、その後が無いので数えない。",
        "b3_th2": ["判定", "件数", "当たり", "その後の最安との差"],
        "b3_sub": "{dir}<span class=\"sub\">{f} 便</span>",
        "b3_none": "まだ答え合わせできる判定がない。",
        "b3_d": "<b>当たりの定義。</b>今買う = その後に5%以上安い値が出なかった。"
                "待つ = その後に2%以上安い値が実際に出た。"
                "様子見 = 約束していないので、後で下がった割合。",
        "b3_x2": "<b>読み方。</b>{since} からの記録のうち、出発日を迎えた {ev} 件"
                 "（どちらの方向でもない路線の {other} 件は除く）。比べる相手は判定した日"
                 "<b>より後</b>に買えた最安。出発の前日・当日の判定は数えない。"
                 "<br><b>件数は同じ便の毎日の判定を重ねた数。</b>確からしさは便の本数で見る。"
                 "記録を始めてまだ日が浅いので、出発の直前数週間に出した判定だけが入っている。",
        "v_buy_now": "今買う", "v_wait": "待つ", "v_watch": "様子見",
        "vd_buy_now": "その後に5%以上安い値が出なかった",
        "vd_wait": "その後に2%以上安い値が実際に出た",
        "vd_watch": "（約束していない。後で下がった割合）",
        "b4_t": "予測した底値の日は当たったか",
        "b4_n": "毎朝記録する「底値はこの日」の予測を、出発後に実際と突き合わせる",
        "b4_n2": "答え合わせ済みの予測 {n} 件（{s} 便）",
        "b4_wait": "<b>まだ答え合わせできる予測がない。</b>予測した底値の日の記録は "
                   "<span class=\"eg\">{since}</span> から。記録した便が出発日を"
                   "迎えると、ここにずれの日数が出る。",
        "b4_days": "{d}日", "b4_k1": "実際の底値の日とのずれ（中央値）",
        "b4_k2": "±{t}日以内に当たった割合",
        "b4_x": "<b>読み方。</b>予測が実際より{t}日以上早すぎたのが "
                "<span class=\"eg\">{e}%</span>、遅すぎたのが "
                "<span class=\"eg\">{l}%</span>。予測した底値の日は、"
                "出発 <span class=\"eg\">{q2}〜{q0}日前</span> に半数が集まる。<br>"
                "<b>この幅が狭いほど、予測は便ごとではなく決まった日を言っている。</b>",
        "b5_t": "予測した日に買ったら、ほかの買い方より安かったか",
        "b5_note": "底値より何%高く払ったか（平均）。低いほど良い",
        "b5_a": "予測した日", "b5_n": "判定の日にすぐ", "b5_d": "{d}日前",
        "b5_l": "単純な買い方",
        "b5_x": "<b>読み方。</b>アプリが予測した底値の日に買うと、底値より平均 "
                "<span class=\"eg\">+{a}%</span>。判定の日にすぐ買うと "
                "<span class=\"eg\">+{n}%</span>、いつも出発{dd}日前に買うと "
                "<span class=\"eg\">+{d}%</span>。予測した日のほうが安かった判定は "
                "<span class=\"eg\">{w}%</span>。<br><b>{verdict}</b>",
        "b5_same": "判定はまだどれも期限の日より出発に近いので、"
                   "「{dd}日前」は「判定の日にすぐ」と同じ値になる。",
        "b5_win": "予測は、単純な買い方より得になっている。",
        "b5_lose": "予測は、単純な買い方に勝てていない。便ごとの予測が役に立っていない。",
        "cav_b1": "<strong>1シーズンぶんの答え合わせ。</strong>出発 {bd0}〜{bd1} の便"
                  "だけで、年末年始や春休みのような繁忙期はまだ含まない。季節が"
                  "変われば底値の位置も動きうるので、アプリの助言を変える前に"
                  "2シーズン目で確かめる。",
        "cav_b2": "<strong>観測を始める前の底は見えない。</strong>観測の始まりは"
                  "出発の中央値 {obs}日前。それより前に底があった系列は、観測の初日が"
                  "底に見える。左端（76日以上前）の山には、これが混ざっている。",
        "acc_sum": "技術指標 — 価格1点ごとの予測誤差の推移",
        # 節1
        "h2_usage": "利用状況の推移",
        "p_usage": "4つは尺度が違うので別々のグラフにしてある。1枚に重ねて2本目の軸を"
                   "立てると、<strong>傾きが軸の取り方で変わってしまう。</strong>"
                   "各グラフの縦軸は、その指標自身の範囲に合わせてある。",
        "c1_t": "モニター総数",    "c1_n": "登録されている監視",
        "c2_t": "新規登録 / 日",   "c2_n": "24時間の作成数",
        "c3_t": "7日以内の再訪率", "c3_n": "一度でも開き直された割合",
        "c4_t": "稼働ルート",      "c4_n": "毎日価格を取りに行く本数",
        "vs_prev": "前日比", "a_trend": "推移",
        "c3_x": "<b>読み方。</b>7日以内に一度でも開き直されたモニターの割合。"
                "いまは <span class=\"eg\">{now}%</span> で、"
                "<span class=\"eg\">{gone}%</span> はこの1週間ひらかれていない。"
                "計測を始めた頃は <span class=\"eg\">{first}%</span> だった。<br>"
                "<b>作って一度見たきり、が大半ということ。</b>"
                "通知を入れたのは、ここを動かすため。",
        # 節2
        "h2_acc": "予測精度の推移",
        "p_acc": "測っているのは<strong>出発30日以内</strong>——人が実際に買う時期。"
                 "全体の数字（誤差0.5%前後）は出発が遠い平坦な予測に薄められていて、"
                 "実態を映さない。8月24日に価格曲線の直前部分を引き直しており、"
                 "その日を縦線で示した。",
        "a1_t": "誤差と偏り（出発30日以内）",
        "a1_n": "真ん中の1件がどれだけ外れるか（中央値）",
        "a1_s1": "絶対誤差", "a1_s2": "偏り（＋は高すぎ）",
        "a1_s3": "絶対誤差 P90", "a1_s4": "偏り P90",
        "a3_t": "外れ方の裾（出発30日以内）",
        "a3_n": "9割の予測が収まるレート（P90）",
        "a2_t": "信頼帯に入った割合",
        "a2_n": "示した価格帯の中に実測が収まった割合",
        "a2_s1": "出発30日以内", "a2_s2": "全体",
        "refit": "曲線を引き直し",
        "too_high": "高め", "too_low": "低め",
        "a1_x": "<b>読み方。</b>実際の価格が <span class=\"eg\">$1,000</span> の便で言うと、"
                "1件ごとの外れ幅は平均 <span class=\"eg\">±${e}</span>。そのうえで平均 "
                "<span class=\"eg\">${b}</span> と{d}に言っている。<br>"
                "<b>2本が重なるほど「外れ＝言いすぎ」で、離れるほど残りはばらつき。</b>"
                "偏りは曲線をずらせば消えるが、誤差は当たり外れの幅なのでモデルを"
                "良くするしかない。直し方が違うので分けて測っている。",
        # 裾。**中央値は「半分は当たっている」としか言っていない。**
        "a3_x": "<b>読み方。</b>9割の予測は誤差 <span class=\"eg\">{p}%</span> "
                "以内に収まっている。<span class=\"eg\">$1,000</span> の便なら "
                "<span class=\"eg\">±${w}</span>。<b>残る1割はこれより"
                "大きく外れる。</b><br>左の中央値（<span class=\"eg\">{m}%</span> "
                "＝ <span class=\"eg\">±${v}</span>）と比べてほしい。"
                "<b>真ん中の1件が当たっていることは、外れた1割がどれだけ"
                "外れたかを何も言っていない。</b>買う人が損をするのは裾のほうで、"
                "縦軸の桁が左のグラフと違うのはそのためにグラフを分けたから。",
        "a3_none": "<b>まだ測っていない。</b>裾の測定は 2026-09-10 から。"
                   "それ以前の日の値は遡って出せない（1件ごとの誤差を"
                   "残していないため）。",
        "a2_x": "<b>読み方。</b>画面に出している価格帯の中に、実際の価格が収まった割合。"
                "いまは <span class=\"eg\">{p}%</span> — <span class=\"eg\">{q}%</span> は"
                "帯の外に出ている。<b>50%を割ると、帯はコイン投げより当たらない。</b><br>"
                "これは偏りとは別の欠陥で、<b>曲線を直しても帯は直らない</b>。"
                "帯の幅は実測のばらつき・観測日数・先読みの長さから別に決めている。",
        "cav_a1": "<strong>この期間、数字は悪化して見えるが、曲線の失敗ではない。</strong>"
                  "精度は7日窓の中央値で、先読み 1・3・7・14・30日 の5点が混ざる。"
                  "8月24日以降に作られた予測が窓の過半を占めるのは<strong>9月下旬</strong>"
                  "——先読み30日ぶんが入れ替わるまで、旧曲線の予測が残り続ける。",
        "cav_a2": "実際、9月1日に予測を新旧で分けて測ると、出発0〜14日前の誤差は"
                  "<strong>旧曲線 27.6% に対し新曲線 3.5%</strong>だった。"
                  "集計値ではなくこの内訳を見ること。",
        # 節3
        "h2_drop": "モニター開始時より価格が下がった路線 — トップ10",
        "p_drop": "観測5日以上の{series}系列のうち、<strong>{dropped}系列</strong>が開始時を下回る"
                  "時点を持つ。下の10件は下落幅の大きい順。棒は開始時からの最大下落率で、"
                  "右側の表に最安が出た時期と現在値を添えた。",
        "d_title": "開始時からの最大下落率", "d_note": "左の目盛は 0% から −70%",
        "d_aria": "開始時からの最大下落率 上位10路線",
        "dep_on": "{d} 発", "dbd": "出発{n}日前", "days": "{n}日",
        "t_start": "開始", "t_low": "最安", "t_drop": "下落", "t_when": "最安は出発",
        "th": ["区間", "出発日", "観測", "開始", "最安", "下落", "最安の時期",
               "現在", "現在の差"],
        "cav_d1": "<strong>「その便が安くなった」ではない。</strong>追っているのは"
                  "「その日その区間で買える最安値」で、便も販売者も入れ替わる。"
                  "−69% は<strong>安い便が新たに現れた</strong>結果であって、"
                  "特定の便の運賃が3分の1になったのではない。",
        "cav_d2": "{dropped}/{series} という多さも割り引いて読む必要がある。「開始時」は1点の"
                  "観測にすぎず、区間最安値は<strong>日々の中央値で 5.27% 動く</strong>。"
                  "初日がたまたま高ければ、その後どこかで下回るのはほぼ確実に起きる。",
        # 元データ
        "raw_sum": "利用状況・精度の数値表（グラフの元データ）",
        "raw_th": ["日付", "モニター", "新規", "作成者", "7日再訪", "稼働ルート",
                   "実測累計", "取得誤り", "直前誤差", "直前偏り", "帯的中"],
        "footer": "出典: S3 の日次 KPI（{span}、{days}日）と本番データベースの"
                  "実測 {recs} 件。価格は取得元が返した最安値で、税・料金を含み"
                  "受託手荷物を含まない。精度の列は 2026-08-24 に計測を始めたため{acc_days}日ぶん。",
    },
    "en": {
        "lang": "en", "dir": "ltr",
        "title": "Flight Price Predictor Operations Dashboard",
        "font": "Inter:wght@400;500;700",
        "family": '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        "eyebrow": "flight-price-predictor · 2026-08-10 → 09-01",
        "h1": "Operations Dashboard",
        "lede": "Built from {days} days of the daily KPI feed and from the production "
                "database. It opens with <strong>the buy-timing check</strong> — the "
                "app exists to say when the fare bottoms out, so this grades that "
                "against flights that have already departed. Then usage, and the legs "
                "that fell below the price we first saw. Point-by-point price error "
                "is folded into the technical section at the end.",
        "k_total": "Monitors", "k_routes": "Active routes",
        "k_recs": "Observations", "k_done": "Completed series",
        "k_subs": "Notification opt-ins",
        "u_item": "", "u_series": "",
        "s_total": "{n} distinct creators", "s_routes": "{research}% are our own study routes",
        "s_recs": "over {days} days", "s_done": "departure date has passed",
        "s_subs": "{p}% of {n} monitors",
        "h2_usage": "How usage has moved",
        "p_usage": "These four sit on different scales, so they get four charts. "
                   "Overlaying them on a second y-axis would "
                   "<strong>let the choice of axis decide the slope.</strong> "
                   "Each chart is scaled to its own measure.",
        "h2_buy": "Buy-timing check — when did the fare actually bottom out?",
        "p_buy": "Graded on <strong>{bn} series</strong> that have departed (departures "
                 "{bd0} to {bd1}, one-way, observed continuously from 60 to 7 days "
                 "out). Observation began a median {obs} days before departure. Prices "
                 "are our own morning observations only; the provider's price history "
                 "is left out, because mixing it in makes far-out days look cheaper.",
        "b_none": "No data for this section yet (the feed predates it).",
        "b_axis": "days before departure →",
        "b_range": "{a}–{b} days out", "b_range_open": "{a}+ days out",
        "b_buy_on": "Bought {d} days out",
        "b_dir": "{t} — {dir}",
        "b_split": "<br>The charts below are split by direction: {jp} series from Japan, "
                   "{us} from the US West Coast ({other} on routes in neither direction "
                   "are left out).",
        "b1_cens": "Only {m} of {bn} series were observed from the start of the window, so "
                   "whether the low came before it cannot be judged yet.",
        "b1_t": "How many days before departure the fare bottomed out",
        "b1_n": "{bn} series (a flat low counts at its last day)",
        "b1_l1": "Series", "b1_l2": "Where the app expects the low ({lo}–{hi} days)",
        "b1_k": "Low here",
        "b1_x": "<b>How to read it.</b> <span class=\"eg\">{vp}%</span> of series "
                "({vn}/{bn}) hit their low in the <span class=\"eg\">{lo}–{hi} day</span> "
                "window the app treats as the low. <span class=\"eg\">{ep}%</span> came "
                "earlier and <span class=\"eg\">{lp}%</span> later.<br><b>{verdict}</b>",
        "b1_early": "The low tends to come earlier than the app assumes.",
        "b1_late": "The low tends to come later than the app assumes.",
        "b1_both": "The low is spread on both sides of the window, not concentrated "
                   "in one period.",
        "b2_t": "How much over the low you paid, by the day you bought",
        "b2_n": "Median. Within {near}% counts as buying at the low",
        "b2_l1": "Day bought", "b2_l2": "Deadline outside Japan→US West ({d} days)",
        "b2_l3": "Smallest gap",
        "b2_k1": "Over the low (median)", "b2_k2": "Mean",
        "b2_k3": "Within {near}% of low", "b2_k4": "Series",
        "b2_x": "<b>How to read it.</b> All directions together. Buying at the "
                "deadline used outside Japan→US West, <span class=\"eg\">{d} days out</span>, paid a median "
                "<span class=\"eg\">+{p}%</span> over the low (<span class=\"eg\">{w}%</span> "
                "got within 5%). The smallest mean gap was at "
                "<span class=\"eg\">{best} days</span>, <span class=\"eg\">+{pb}%</span>. "
                "Waiting until <span class=\"eg\">{last} days</span> cost "
                "<span class=\"eg\">+{pl}%</span>.<br><b>Too early and too late both "
                "cost.</b> If a few days either side of the deadline changes the gap a "
                "lot, that is evidence for moving the deadline itself.",
        "b7_t": "By direction: % over the low, by the day you bought — {dir}",
        "b7_n": "Median. {s} series",
        "b7_none": "<b>No series in this direction yet.</b>",
        "b7_l2": "Deadline for this direction ({d} days)",
        "b7_x": "<b>How to read it.</b> Buying at this direction's deadline, "
                "<span class=\"eg\">{d} days out</span>, paid a median "
                "<span class=\"eg\">+{p}%</span> over the low (<span class=\"eg\">{w}%</span> "
                "within 5%). The smallest mean gap was at <span class=\"eg\">{best} days</span> "
                "(<span class=\"eg\">+{pb}%</span>); waiting until "
                "<span class=\"eg\">{last} days</span> cost <span class=\"eg\">+{pl}%</span>."
                "<br><b>If the deadline and the best day sit far apart, the deadline "
                "needs another look.</b>",
        "b7_x_nr": "<b>How to read it.</b> <b>No series has been observed yet</b> at this "
                   "direction's deadline, <span class=\"eg\">{d} days out</span>. The smallest "
                   "mean gap was at <span class=\"eg\">{best} days</span> "
                   "(<span class=\"eg\">+{pb}%</span>); waiting until "
                   "<span class=\"eg\">{last} days</span> cost <span class=\"eg\">+{pl}%</span>.",
        "dir_JP->US-West": "Japan → US West Coast",
        "dir_US-West->JP": "US West Coast → Japan",
        "b3_t": "Were the app's calls right?",
        "b3_n": "Buy now / wait / watch, logged every morning and graded after departure",
        "b3_wait": "<b>No call can be graded yet.</b> Logging began on "
                   "<span class=\"eg\">{since}</span>. Once those flights depart, the "
                   "hit rate shows here.",
        "b3_th": ["Call", "Count", "Right", "Gap to later low", "Counts as right when"],
        "b3_x": "<b>How to read it.</b> <span class=\"eg\">{ev}</span> calls logged "
                "since {since} whose flights have departed. Each is compared with the "
                "cheapest fare buyable <b>after</b> the call — you cannot go back. "
                "Calls made on the day before or the day of departure are left out: "
                "nothing comes after them.",
        "b3_th2": ["Call", "Count", "Right", "Gap to later low"],
        "b3_sub": "{dir}<span class=\"sub\">{f} flights</span>",
        "b3_none": "No call can be graded yet.",
        "b3_d": "<b>What counts as right.</b> Buy now: nothing 5%+ cheaper appeared afterwards. "
                "Wait: something 2%+ cheaper did appear. "
                "Watch: no promise made, so this is the share that fell later.",
        "b3_x2": "<b>How to read it.</b> {ev} calls logged since {since} on flights that have "
                 "departed ({other} on routes in neither direction are left out). The "
                 "comparison is the lowest fare still available <b>after</b> the call. Calls "
                 "from the last two days before departure are not counted."
                 "<br><b>Counts stack the same flight's daily calls.</b> Judge certainty by the "
                 "number of flights. Logging is recent, so only calls from the last few weeks "
                 "before departure are in here.",
        "v_buy_now": "Buy now", "v_wait": "Wait", "v_watch": "Watch",
        "vd_buy_now": "nothing 5%+ cheaper appeared afterwards",
        "vd_wait": "something 2%+ cheaper did appear",
        "vd_watch": "(no promise made; share that fell later)",
        "b4_t": "Did the predicted low date hold?",
        "b4_n": "Each morning's “the low will be on this day” call, checked after departure",
        "b4_n2": "{n} predictions graded ({s} flights)",
        "b4_wait": "<b>No prediction can be graded yet.</b> Predicted low dates have "
                   "been logged since <span class=\"eg\">{since}</span>. Once those "
                   "flights depart, the gap in days shows here.",
        "b4_days": "{d} d", "b4_k1": "gap from the actual low (median)",
        "b4_k2": "within ±{t} days",
        "b4_x": "<b>How to read it.</b> <span class=\"eg\">{e}%</span> of predictions "
                "were more than {t} days too early, <span class=\"eg\">{l}%</span> too "
                "late. Half the predicted low dates sit between "
                "<span class=\"eg\">{q2} and {q0} days</span> out.<br><b>The narrower "
                "that range, the more the prediction is a fixed day rather than a "
                "per-flight call.</b>",
        "b5_t": "Buying on the predicted day vs simpler rules",
        "b5_note": "Average % paid over the low. Lower is better",
        "b5_a": "Predicted day", "b5_n": "Right away", "b5_d": "{d} days out",
        "b5_l": "Simple rule",
        "b5_x": "<b>How to read it.</b> Buying on the app's predicted low date paid "
                "<span class=\"eg\">+{a}%</span> over the low on average. Buying on the "
                "day of the call paid <span class=\"eg\">+{n}%</span>; always buying "
                "{dd} days out paid <span class=\"eg\">+{d}%</span>. The predicted day "
                "was cheaper in <span class=\"eg\">{w}%</span> of calls.<br>"
                "<b>{verdict}</b>",
        "b5_same": "Every call so far came after the deadline, so “{dd} days out” "
                   "equals “right away”.",
        "b5_win": "The prediction beats the simple rules.",
        "b5_lose": "The prediction does not beat the simple rules; the per-flight "
                   "call is not adding value.",
        "cav_b1": "<strong>One season of evidence.</strong> Only departures from {bd0} "
                  "to {bd1}; no peak period such as New Year or spring break yet. The "
                  "low may sit elsewhere in another season, so a second season should "
                  "confirm this before the app's advice changes.",
        "cav_b2": "<strong>A low before observation began is invisible.</strong> "
                  "Observation started a median {obs} days out. A series whose real low "
                  "came earlier shows its first observed day as the low; the left-hand "
                  "bar (76+ days) includes some of these.",
        "acc_sum": "Technical — how point-by-point price error has moved",
        "c1_t": "Monitors", "c1_n": "watches on the books",
        "c2_t": "New per day", "c2_n": "created in 24 hours",
        "c3_t": "Reopened within 7 days", "c3_n": "opened again at least once",
        "c4_t": "Active routes", "c4_n": "fetched every morning",
        "vs_prev": "vs prev day", "a_trend": "trend",
        "c3_x": "<b>How to read it.</b> The share of monitors opened again at least "
                "once within seven days. Right now <span class=\"eg\">{now}%</span>, "
                "so <span class=\"eg\">{gone}%</span> went untouched all week. It was "
                "<span class=\"eg\">{first}%</span> when measurement began.<br>"
                "<b>Most people look once and never come back.</b> "
                "Notifications exist to move this number.",
        "h2_acc": "How prediction accuracy has moved",
        "p_acc": "What is measured is <strong>within 30 days of departure</strong> — "
                 "when people actually buy. The all-in figure (around 0.5% error) is "
                 "diluted by flat, far-out predictions and does not describe reality. "
                 "The last-minute part of the price curve was refitted on 24 August; "
                 "that day is marked with a vertical rule.",
        "a1_t": "Error and bias (within 30 days)",
        "a1_n": "how far off the middle prediction is (median)",
        "a1_s1": "Absolute error", "a1_s2": "Bias (+ = predicted too high)",
        "a1_s3": "Absolute error P90", "a1_s4": "Bias P90",
        "a3_t": "The tail (within 30 days)",
        "a3_n": "the rate 9 predictions in 10 stay within (P90)",
        "a2_t": "Share landing inside the band",
        "a2_n": "how often the actual price fell inside the range we showed",
        "a2_s1": "Within 30 days", "a2_s2": "All predictions",
        "refit": "curve refitted",
        "too_high": "too high", "too_low": "too low",
        "a1_x": "<b>How to read it.</b> On a <span class=\"eg\">$1,000</span> fare, a typical "
                "prediction misses by <span class=\"eg\">\u00b1${e}</span> either way \u2014 and on "
                "average we say <span class=\"eg\">${b}</span>, which is {d}.<br>"
                "<b>The closer the two lines, the more of the miss is systematic; the wider the "
                "gap, the more of it is scatter.</b> Bias goes away if the curve is shifted; error "
                "does not \u2014 that needs a better model. They are measured apart because they "
                "are fixed apart.",
        # The tail. **A median only says half of them were close.**
        "a3_x": "<b>How to read it.</b> Nine predictions in ten land within "
                "<span class=\"eg\">{p}%</span> \u2014 on a "
                "<span class=\"eg\">$1,000</span> fare, inside "
                "<span class=\"eg\">\u00b1${w}</span>. <b>The remaining tenth misses "
                "by more than that.</b><br>Compare the median on the left "
                "(<span class=\"eg\">{m}%</span>, or <span class=\"eg\">\u00b1${v}</span>). "
                "<b>A middling prediction being close says nothing about how far the "
                "missed tenth strays.</b> The tail is what costs a buyer money. Note the "
                "vertical scale differs from the chart on the left \u2014 that is why "
                "they are separate charts.",
        "a3_none": "<b>Not measured yet.</b> The tail has been recorded since "
                   "10 September 2026. Earlier days cannot be filled in, because "
                   "per-prediction errors were not kept.",
        "a2_x": "<b>How to read it.</b> How often the actual price landed inside the range shown "
                "on screen. Right now <span class=\"eg\">{p}%</span> did, so "
                "<span class=\"eg\">{q}%</span> fell outside. <b>Below 50% the range is worse than "
                "a coin flip.</b><br>This is a separate fault from bias: <b>correcting the curve "
                "does not correct the range.</b> Its width comes from observed spread, how many "
                "days we have watched, and how far ahead we are looking.",
        "cav_a1": "<strong>The numbers look worse across this period, but that is not "
                  "the curve failing.</strong> Accuracy is a median over a 7-day "
                  "window, mixing five lead times: 1, 3, 7, 14 and 30 days. "
                  "Predictions made after 24 August only take over the window in "
                  "<strong>late September</strong> — until the 30-day lead turns over, "
                  "old-curve predictions keep being scored.",
        "cav_a2": "Split by curve on 1 September, the error 0–14 days before departure "
                  "was <strong>27.6% on the old curve against 3.5% on the new one</strong>. "
                  "Read that split, not the aggregate.",
        "h2_drop": "Legs that fell below their starting price — top 10",
        "p_drop": "Of the {series} series with five or more observations, "
                  "<strong>{dropped}</strong> dipped below the first price we recorded. "
                  "The ten below are the deepest. Bars show the largest drop from the "
                  "starting price; the table adds when the low landed and where the "
                  "price stands now.",
        "d_title": "Largest drop from the starting price",
        "d_note": "scale runs 0% to −70%",
        "d_aria": "Ten legs with the largest drop from their starting price",
        "dep_on": "dep {d}", "dbd": "{n}d before dep", "days": "{n}d",
        "t_start": "start", "t_low": "low", "t_drop": "drop", "t_when": "low landed",
        "th": ["Leg", "Departure", "Obs.", "Start", "Low", "Drop", "Low landed",
               "Now", "Now vs start"],
        "cav_d1": "<strong>This is not “that flight got cheaper”.</strong> What we "
                  "track is the cheapest fare buyable on that leg that day, and both "
                  "the flight and the seller change underneath it. A −69% reading means "
                  "<strong>a cheaper flight appeared</strong>, not that one fare fell "
                  "to a third of its price.",
        "cav_d2": "The count itself — {dropped} of {series} — needs discounting too. The "
                  "“starting price” is a single observation, and the cheapest fare on a "
                  "leg moves by <strong>5.27% a day at the median</strong>. If day one "
                  "happened to be high, dipping below it later is near-certain.",
        "raw_sum": "Underlying numbers for usage and accuracy",
        "raw_th": ["Date", "Monitors", "New", "Creators", "7d return", "Routes",
                   "Observations", "Fetch errors", "Error (30d)", "Bias (30d)",
                   "Band hit"],
        "footer": "Sources: the daily KPI feed on S3 ({span}, {days} days) and "
                  "{recs} observed prices from the production database. Prices are the "
                  "cheapest the provider returned; they include taxes and fees and "
                  "exclude checked baggage. The accuracy columns start on 2026-08-24, "
                  "so they cover {acc_days} days.",
    },
}

MARKUP = """<div class="wrap">

  <header>
    <div class="eyebrow">{eyebrow}</div>
    <h1>{h1}</h1>
    <p class="lede">{lede}</p>
  </header>

  <dl class="strip" id="strip"></dl>

  <section>
    <div class="sec-head">
      <h2>{h2_buy}</h2>
      <p id="buyLede"></p>
    </div>
    <div id="buy" class="stack"></div>
    <div class="caveat" id="buyCaveat"></div>
  </section>

  <section>
    <div class="sec-head">
      <h2>{h2_usage}</h2>
      <p>{p_usage}</p>
    </div>
    <div class="grid-4" id="usage"></div>
  </section>

  <section>
    <div class="sec-head">
      <h2>{h2_drop}</h2>
      <p>{p_drop}</p>
    </div>
    <div class="card">
      <p class="chart-title">{d_title}</p>
      <p class="chart-note">{d_note}</p>
      <div id="drops"></div>
    </div>
    <div class="tablebox">
      <table id="dropTable">
        <thead><tr>{th}</tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="caveat">
      <p>{cav_d1}</p>
      <p>{cav_d2}</p>
    </div>
  </section>

  <!-- 点の予測誤差は目的（底値の時期）の指標ではない。技術者向けに畳む。 -->
  <details class="tech">
    <summary>{acc_sum}</summary>
    <section class="tech-body">
      <div class="sec-head">
        <h2>{h2_acc}</h2>
        <p>{p_acc}</p>
      </div>
      <div class="grid-2" id="accuracy"></div>
      <div class="caveat">
        <p>{cav_a1}</p>
        <p>{cav_a2}</p>
      </div>
    </section>
  </details>

  <details>
    <summary>{raw_sum}</summary>
    <div class="tablebox">
      <table id="rawTable">
        <thead><tr>{raw_th}</tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </details>

  <footer>{footer}</footer>
</div>

<div class="tip" id="tip"></div>
"""


def patched_js() -> str:
    js = JS_SRC
    for old, new in JS_PATCHES:
        if old not in js:
            raise SystemExit("描画コードに見つからない:\n" + old[:90])
        js = js.replace(old, new, 1)
    # 表の見出しは markup 側で出すので、JS 内の残りは無い想定。
    left = re.findall(r"[ぁ-んァ-ヶ一-龠]", re.sub(r"/\*.*?\*/", "", js, flags=re.S)
                      .replace("//", "\n//"))
    return js


# 差し込み口を持ったままの元の文言。**fill_strings はここから作り直す。**
_TEMPLATES = copy.deepcopy(STRINGS)


def fill_strings(payload: dict) -> None:
    """本文と脚注の数字を、その日のデータで埋める。**焼き込まない。**

    2026-09-10 まで、そう書いてあるのに埋めていたのは eyebrow だけだった。
    本文と脚注は最初に書いた日の値のまま10日間出ており、グラフが 32 日ぶんを
    描いている隣で「23日ぶん」「44,178 件」と言っていた。
    **画面が自分の数字と食い違うのがいちばんまずい。**

    **HTML を作る経路すべてがここを通ること。** 同じ日に、CLI 側だけ直して
    本番の書き出し（app/dashboard_build.py）を直し忘れ、S3 には波括弧が
    そのまま出た。埋める場所は1つにする。
    """
    # **元の文言から作り直す。** STRINGS を直接書き換えると、差し込み口が
    # 1回目の呼び出しで消える。長く動いているプロセス（uvicorn）では
    # 2回目以降が1回目の数字を出し続けることになり、焼き込みと同じことが
    # 起きる。試験でも、1つ目の試験が2つ目を汚していた。
    for lang, template in _TEMPLATES.items():
        STRINGS[lang] = copy.deepcopy(template)

    m, kpi = payload["meta"], payload["kpi"]
    span = f"2026-{kpi[0]['d']} → {kpi[-1]['d']}"
    # 精度の日数は総日数ではない。計測を始めたのが遅いので、
    # **値が入っている日を数える。**
    acc_days = sum(1 for r in kpi if r["aerr"] is not None)
    # 取得口が古いと research_routes が無い。**その1つのために全部を
    # 落とさない。** 分からないときは「—」と出す。
    rr, rt = m.get("research_routes"), m.get("routes")
    values = {
        "span": span, "days": len(kpi), "acc_days": acc_days,
        "recs": f"{m.get('recs', 0):,}", "series": m.get("series", "—"),
        "dropped": m.get("dropped", "—"),
        "research": round(rr / rt * 100) if rr is not None and rt else "—",
    }
    for t in STRINGS.values():
        t["eyebrow"] = f"flight-price-predictor · {span}"
        for key, text in list(t.items()):
            if isinstance(text, str) and "{" in text:
                for name, value in values.items():
                    text = text.replace("{" + name + "}", str(value))
                t[key] = text


def build(lang: str) -> str:
    t = STRINGS[lang]
    css = CSS.replace(
        '"Zen Kaku Gothic New", -apple-system, BlinkMacSystemFont,\n'
        '            "Hiragino Sans", "Noto Sans JP", sans-serif', t["family"])
    body = MARKUP.format(
        **{k: v for k, v in t.items() if isinstance(v, str)},
        th="".join(f"<th>{h}</th>" for h in t["th"]),
        raw_th="".join(f"<th>{h}</th>" for h in t["raw_th"]),
    )
    strings = json.dumps({k: v for k, v in t.items()},
                         ensure_ascii=False, separators=(",", ":"))
    return (
        f"<title>{t['title']}</title>\n"
        f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        f'family={t["font"]}&family=IBM+Plex+Mono:wght@400;500;600&display=swap">\n'
        f"<style>{css}</style>\n\n{body}\n"
        f'<script id="data" type="application/json">{PAYLOAD}</script>\n'
        f'<script id="strings" type="application/json">{strings}</script>\n'
        "<script>\n(function () {"
        + patched_js().replace(
            "  const D = JSON.parse(document.getElementById('data').textContent);",
            "  const D = JSON.parse(document.getElementById('data').textContent);\n"
            "  const S = JSON.parse(document.getElementById('strings').textContent);", 1)
        + "})();\n</script>\n"
    )


def to_payload(ops: dict) -> dict:
    """取得口の生データを、画面が読む形に詰め替える。

    **空欄は null のまま運ぶ。** 0 に化けるとグラフが原点まで落ちて、
    「計測を始める前」と「値がゼロ」を見分けられなくなる。
    """
    kpi = []
    for r in ops["kpi"]:
        total = r["watches_total"]
        a7 = r["watches_accessed_last_7d"]
        kpi.append({
            "d": r["date"][5:],
            "total": total,
            "new": r["watches_created_last_24h"],
            "creators": r["distinct_creators"],
            "ret7": round(a7 / total * 100, 1) if a7 is not None and total else None,
            "routes": r["routes_active"],
            "recs": r["snapshots_real"],
            "errs": r["fetch_errors"],
            "done": r["series_completed"],
            "subs": r.get("push_subscriptions"),
            "aerr": r.get("prediction_abs_error_pct_near"),
            "abias": r.get("prediction_bias_pct_near"),
            "aband": r.get("prediction_band_hit_pct_near"),
            # 裾（2026-09-10 から）。それ以前の行には無いので None が入る。
            # **グラフ側は欠けた点を飛ばして描く。** 揃っていない日を
            # 捨てると、それ以前の履歴がまるごと消える。
            "aerrp": r.get("prediction_abs_error_p90_pct_near"),
            "abiasp": r.get("prediction_bias_p90_pct_near"),
            "oerr": r.get("prediction_abs_error_pct"),
            "oband": r.get("prediction_band_hit_pct"),
        })
    m = dict(ops["meta"])
    m["from"], m["to"] = kpi[0]["d"], kpi[-1]["d"]
    # 買い時の検証（2026-10-12 から）。古い取得口には無いので、無ければ null。
    return {"kpi": kpi, "drops": ops["drops"], "meta": m,
            "buy": ops.get("buy_timing")}


def main() -> None:
    global PAYLOAD, STRINGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=pathlib.Path, required=True,
                    help="/api/ops/dashboard.json の応答")
    ap.add_argument("--out-dir", type=pathlib.Path, default=HERE)
    args = ap.parse_args()

    ops = json.loads(args.data.read_text(encoding="utf-8"))
    payload = to_payload(ops)
    PAYLOAD = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    fill_strings(payload)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for lang, name in (("ja", "fpp-dashboard.html"),
                       ("en", "fpp-dashboard.en.html")):
        out = args.out_dir / name
        out.write_text(build(lang), encoding="utf-8")
        print(f"  {name}  {out.stat().st_size / 1024:.0f} KB  ({lang})")


if __name__ == "__main__":
    main()
