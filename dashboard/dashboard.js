
  "use strict";
  const D = JSON.parse(document.getElementById('data').textContent);
  const tip = document.getElementById('tip');
  const NS = 'http://www.w3.org/2000/svg';
  const el = (n, a) => { const e = document.createElementNS(NS, n);
    for (const k in a) e.setAttribute(k, a[k]); return e; };

  function showTip(evt, html) {
    tip.innerHTML = html; tip.style.opacity = '1';
    const r = tip.getBoundingClientRect();
    let x = evt.clientX + 14, y = evt.clientY - r.height - 10;
    if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - 14;
    if (y < 8) y = evt.clientY + 18;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  const hideTip = () => { tip.style.opacity = '0'; };

  /* ── 状態帯 ── */
  const m = D.meta;
  const cells = [
    ['監視中のモニター', m.total, '件', `作成者 ${m.creators} 人`],
    ['稼働ルート', m.routes, '/1000', '54% は調査用'],
    ['実測レコード', m.recs.toLocaleString(), '', '観測 23 日'],
    ['完了した系列', m.done, '本', '出発日を過ぎたもの'],
    ['通知の購読', m.subs, '件', `${m.total} 件中 ${(m.subs / m.total * 100).toFixed(0)}%`],
  ];
  document.getElementById('strip').innerHTML = cells.map(([k, v, u, s]) =>
    `<div class="cell"><dt>${k}</dt><dd>${v}<span class="u">${u}</span></dd>
     <span class="sub">${s}</span></div>`).join('');

  /* ── 買い時の検証（このアプリの目的そのもの）──
     精度欄の「明日の価格を何%で当てたか」は、目的（底値の時期を言い当てる）と
     ずれていた（2026-10-12、利用者の指摘）。ここでは出発日を迎えた系列で
     「底値はいつ来たか」「◯日前に買ったら底値より何%高かったか」を見せる。
     **文言はすべて S から。** 日本語をここに書くと英語版に漏れる。 */
  (function () {
    const host = document.getElementById('buy');
    const B = D.buy;
    const fill = (t, o) => Object.keys(o).reduce(
      (a, k) => a.split('{' + k + '}').join(o[k]), t);
    // **古い取得口には無い。** 1つの欠けで残りのグラフまで止めない。
    if (!B || !B.series) {
      document.getElementById('buyLede').innerHTML = S.b_none;
      return;
    }
    // 方向別（2026-11-04、課題16）。底値の傾向が日本発と米国発で違うので、全方向を
    // 混ぜた図は出さない。**古い取得口には dr.bottoms が無い** ので、そのときは全体の図。
    const dirs = (B.by_direction || []).filter((dr) => dr.bottoms);
    const dirName = (dr) => S['dir_' + dr.direction] || dr.direction;
    const inDirs = dirs.reduce((a, dr) => a + dr.series, 0);
    document.getElementById('buyLede').innerHTML = fill(S.p_buy, {
      bn: B.series, bd0: B.departures[0], bd1: B.departures[1],
      obs: B.obs_start_median }) + (dirs.length === 2 ? fill(S.b_split, {
        jp: dirs[0].series, us: dirs[1].series, other: B.series - inDirs }) : '');

    const grid = document.createElement('div');
    grid.className = 'grid-2'; host.appendChild(grid);
    const card = () => { const c = document.createElement('div');
      c.className = 'card'; grid.appendChild(c); return c; };

    // 縦棒。左が出発から遠く、右が出発直前。2枚とも同じ向きにそろえる。
    function columns(c, o) {
      if (!c) { c = document.createElement('div'); c.className = 'card'; }
      const W = 320, H = 190, P = { t: 18, r: 8, b: 34, l: 34 };
      // 目盛は 1・2・5 の刻み。7, 15, 22 のような目盛は読みにくい。
      const top = Math.max(...o.rows.map((r) => r.v || 0)) * 1.12 || 1;
      const mag = Math.pow(10, Math.floor(Math.log10(top / 4)));
      const step = [1, 2, 5, 10].map((f) => f * mag).find((x) => x * 4 >= top);
      const vmax = step * 4;
      const bw = (W - P.l - P.r) / o.rows.length;
      const Y = (v) => P.t + (1 - v / vmax) * (H - P.t - P.b);
      const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
        'aria-label': o.title });
      for (let k = 0; k <= 4; k++) {
        const v = vmax * k / 4, y = Y(v);
        svg.appendChild(el('line', { x1: P.l, x2: W - P.r, y1: y, y2: y,
          class: 'gridline' }));
        const t = el('text', { x: P.l - 5, y: y + 3, 'text-anchor': 'end',
          class: 'axis-txt' });
        t.textContent = o.fmt(v, true); svg.appendChild(t);
      }
      o.rows.forEach((r, k) => {
        const x = P.l + k * bw;
        const lbl = el('text', { x: x + bw / 2, y: H - P.b + 12,
          'text-anchor': 'middle', class: 'axis-txt' + (r.tone !== 's1' ? ' ink' : '') });
        lbl.textContent = r.label; svg.appendChild(lbl);
        if (r.v === null || r.v === undefined) return;
        const h = Math.max(H - P.b - Y(r.v), r.v > 0 ? 2 : 0);
        // 上端だけ丸める（底辺は基線に付ける）。
        const w = bw - 4, y = H - P.b - h, rr = Math.min(3, w / 2, h);
        svg.appendChild(el('path', { class: 'fl-' + r.tone,
          d: `M${x + 2},${H - P.b} V${y + rr} Q${x + 2},${y} ${x + 2 + rr},${y}` +
             ` H${x + 2 + w - rr} Q${x + 2 + w},${y} ${x + 2 + w},${y + rr}` +
             ` V${H - P.b} Z` }));
        if (r.tag) {
          const t = el('text', { x: x + bw / 2, y: y - 5, 'text-anchor': 'middle',
            class: 'axis-txt ink', 'font-weight': 600 });
          // 値札は整数に丸める。小数まで出すと隣の棒の値札とぶつかる
          // （+13.4% と +14.5% が重なった）。小数は吹き出しで見せる。
          t.textContent = o.fmt(r.v, true); svg.appendChild(t);
        }
        // 当たり判定は棒より大きく（列の全高）。
        const hit = el('rect', { x, y: P.t, width: bw, height: H - P.t - P.b,
          fill: 'transparent' });
        hit.addEventListener('pointermove', (e) => showTip(e, r.tip));
        hit.addEventListener('pointerleave', hideTip);
        svg.appendChild(hit);
      });
      const ax = el('text', { x: W - P.r, y: H - 4, 'text-anchor': 'end',
        class: 'axis-txt' });
      ax.textContent = o.axis === undefined ? S.b_axis : o.axis;
      svg.appendChild(ax);
      c.innerHTML = `<p class="chart-title">${o.title}</p>` +
                    `<p class="chart-note">${o.note}</p>`;
      c.appendChild(svg);
      c.insertAdjacentHTML('beforeend', '<div class="legend">' + o.legend.map(
        ([tone, name]) => `<span><i class="swatch sw-${tone}"></i>${name}</span>`)
        .join('') + '</div>');
      c.insertAdjacentHTML('beforeend', `<p class="explain">${o.explain}</p>`);
      return c;
    }

    // 1枚目: 実際の底値の位置
    function bottomsChart(c, title, bottoms, n, valley, observed) {
      const lo = valley[0], hi = valley[1];
      // 谷より前・谷の中・谷より後。**結論の一文もデータから選ぶ。**
      // 最初に「早く来ることが多い」と固定で書いたが、横ばいの底を最後の日で
      // 数えると、本番の 72 系列では前 39%・後 43% で成り立たなかった。
      const sum = (f) => bottoms.filter(f).reduce((a, b) => a + b.n, 0);
      const early = sum((b) => b.lo > hi), late = sum((b) => b.hi !== null && b.hi < lo);
      const inValley = bottoms.find((b) => b.valley);
      // 谷の初めまで観測が届かない系列があると、谷より前の底が見えず「遅く来る」に偏る。
      const tilt = observed < n ? fill(S.b1_cens, { m: observed, bn: n })
        : Math.abs(early - late) <= 0.25 * Math.max(early, late)
          ? S.b1_both : early > late ? S.b1_early : S.b1_late;
      const share = (k) => Math.round(k / n * 100);
      const mode = bottoms.reduce((a, b) => (b.n > a.n ? b : a), bottoms[0]);
      columns(c, {
        title, note: fill(S.b1_n, { bn: n }),
        fmt: (v, ax) => (ax ? Math.round(v) : v) + '',
        legend: [['s1', S.b1_l1], ['s3', fill(S.b1_l2, { lo, hi })]],
        rows: bottoms.map((b) => ({
          label: b.hi === null ? b.lo + '+' : b.lo + '–' + b.hi,
          v: b.n, tone: b.valley ? 's3' : 's1', tag: b.valley || b === mode,
          tip: `<b>${fill(b.hi === null ? S.b_range_open : S.b_range,
                          { a: b.lo, b: b.hi })}</b><br>` +
               `<span class="k">${S.b1_k}</span><b>${b.n} / ${n}</b>` +
               ` <span class="k">(${share(b.n)}%)</span>` })),
        explain: fill(S.b1_x, { bn: n, lo, hi,
          vn: inValley ? inValley.n : 0, vp: share(inValley ? inValley.n : 0),
          ep: share(early), lp: share(late), verdict: tilt }),
      });
    }
    if (dirs.length) {
      dirs.forEach((dr) => {
        const title = fill(S.b_dir, { t: S.b1_t, dir: dirName(dr) });
        const c = card();
        if (!dr.series) {
          c.innerHTML = `<p class="chart-title">${title}</p>` +
            `<p class="explain">${S.b7_none}</p>`;
          return;
        }
        bottomsChart(c, title, dr.bottoms, dr.series, dr.valley, dr.valley_observed);
      });
    } else {
      bottomsChart(card(), S.b1_t, B.bottoms, B.series, B.valley, B.series);

    // 2枚目: ◯日前に買ったら、底値より何%高かったか
    const bd = B.buy_days.filter((b) => b.n);
    const at = (d) => bd.find((b) => b.dbd === d);
    const rise = at(B.rise_dbd), best = at(B.best_dbd), last = bd[bd.length - 1];
    columns(card(), {
      title: S.b2_t, note: fill(S.b2_n, { near: B.near_pct }),
      fmt: (v, ax) => (v > 0 ? '+' : '') + (ax ? Math.round(v) : v.toFixed(1)) + '%',
      legend: [['s1', S.b2_l1], ['s3', fill(S.b2_l2, { d: B.rise_dbd })],
               ['s2', S.b2_l3]],
      rows: B.buy_days.map((b) => ({
        label: b.dbd + '', v: b.median_pct,
        tone: b.dbd === B.rise_dbd ? 's3' : b.dbd === B.best_dbd ? 's2' : 's1',
        tag: b.dbd === B.rise_dbd || b.dbd === B.best_dbd || b === last,
        tip: `<b>${fill(S.b_buy_on, { d: b.dbd })}</b><br>` +
             `<span class="k">${S.b2_k1}</span><b>+${b.median_pct}%</b><br>` +
             `<span class="k">${S.b2_k2}</span><b>+${b.mean_pct}%</b><br>` +
             `<span class="k">${fill(S.b2_k3, { near: B.near_pct })}</span>` +
             `<b>${b.within_pct}%</b><br>` +
             `<span class="k">${S.b2_k4}</span><b>${b.n}</b>` })),
      explain: fill(S.b2_x, {
        d: B.rise_dbd, p: rise ? rise.median_pct : '—',
        w: rise ? rise.within_pct : '—',
        best: B.best_dbd, pb: best ? best.median_pct : '—',
        last: last.dbd, pl: last.median_pct }),
    });
    }

    // 方向別（2026-10-14、課題16）。底値の時期は日本発と米国発で違うので、
    // 同じ表を方向ごとに出す。期限はその方向に当てている値。
    if (B.by_direction && B.by_direction.length) {
      const g3 = document.createElement('div');
      g3.className = 'grid-2'; host.appendChild(g3);
      B.by_direction.forEach((dr) => {
        const name = S['dir_' + dr.direction] || dr.direction;
        const c = document.createElement('div');
        c.className = 'card'; g3.appendChild(c);
        if (!dr.series) {
          c.innerHTML = `<p class="chart-title">${fill(S.b7_t, { dir: name })}</p>` +
            `<p class="explain">${S.b7_none}</p>`;
          return;
        }
        const at = (d) => dr.buy_days.find((b) => b.dbd === d && b.n);
        const rr = at(dr.rise_dbd), bb = at(dr.best_dbd);
        const lastd = dr.buy_days.filter((b) => b.n).slice(-1)[0];
        columns(c, {
          title: fill(S.b7_t, { dir: name }), note: fill(S.b7_n, { s: dr.series }),
          fmt: (v, ax) => (v > 0 ? '+' : '') + (ax ? Math.round(v) : v.toFixed(1)) + '%',
          legend: [['s1', S.b2_l1], ['s3', fill(S.b7_l2, { d: dr.rise_dbd })],
                   ['s2', S.b2_l3]],
          rows: dr.buy_days.map((b) => ({
            label: b.dbd + '', v: b.n ? b.median_pct : null,
            tone: b.dbd === dr.rise_dbd ? 's3' : b.dbd === dr.best_dbd ? 's2' : 's1',
            tag: b.dbd === dr.rise_dbd || b.dbd === dr.best_dbd,
            tip: `<b>${fill(S.b_buy_on, { d: b.dbd })}</b><br>` +
                 `<span class="k">${S.b2_k1}</span><b>+${b.median_pct}%</b><br>` +
                 `<span class="k">${S.b2_k2}</span><b>+${b.mean_pct}%</b><br>` +
                 `<span class="k">${fill(S.b2_k3, { near: B.near_pct })}</span>` +
                 `<b>${b.within_pct}%</b><br>` +
                 `<span class="k">${S.b2_k4}</span><b>${b.n}</b>` })),
          // 期限の日にまだ観測が無いことがある（自前の実測は出発60日前から）。
          explain: fill(rr ? S.b7_x : S.b7_x_nr, {
            d: dr.rise_dbd, p: rr ? rr.median_pct : '—', w: rr ? rr.within_pct : '—',
            best: dr.best_dbd, pb: bb ? bb.mean_pct : '—',
            last: lastd.dbd, pl: lastd.median_pct }),
        });
      });
    }

    // 3枚目: アプリの判定（今買う／待つ／様子見）の答え合わせ
    const V = B.verdicts || { evaluable: 0, by_verdict: {} };
    const vc = document.createElement('div');
    vc.className = 'card'; host.appendChild(vc);
    let body;
    // 方向別（2026-11-04、課題16）。底値の傾向が日本発と米国発で違うので、当否も
    // 分けて数える。古い取得口には dr.verdicts が無いので、そのときは全体の表を出す。
    const split = (B.by_direction || []).filter((dr) => dr.verdicts);
    if (!V.evaluable) {
      body = `<p class="explain">${fill(S.b3_wait, { since: V.since || '—' })}</p>`;
    } else if (split.length) {
      const order = ['buy_now', 'wait', 'watch'];
      const tables = split.map((dr) => {
        const W = dr.verdicts;
        const name = S['dir_' + dr.direction] || dr.direction;
        const head = `<p class="verd-dir">${fill(S.b3_sub, { dir: name, f: W.flights })}</p>`;
        if (!W.evaluable) return `<div>${head}<p class="explain">${S.b3_none}</p></div>`;
        return `<div>${head}<div class="tablebox"><table class="verd"><thead><tr>` +
          S.b3_th2.map((h) => `<th>${h}</th>`).join('') + '</tr></thead><tbody>' +
          order.filter((k) => W.by_verdict[k]).map((k) => {
            const v = W.by_verdict[k];
            return `<tr><td>${S['v_' + k]}</td><td class="num">${v.n}</td>` +
              `<td class="num">${v.hit_pct === null ? '—' : v.hit_pct + '%'}</td>` +
              `<td class="num">+${v.median_regret_pct.toFixed(1)}%</td></tr>`;
          }).join('') + '</tbody></table></div></div>';
      }).join('');
      const inSplit = split.reduce((a, dr) => a + dr.verdicts.evaluable, 0);
      body = `<div class="grid-2 verd-grid">${tables}</div>` +
        `<p class="explain">${S.b3_d}<br>` + fill(S.b3_x2, {
          since: V.since, ev: inSplit, other: V.evaluable - inSplit }) + '</p>';
    } else {
      const order = ['buy_now', 'wait', 'watch'];
      body = '<div class="tablebox"><table class="narrow"><thead><tr>' +
        S.b3_th.map((h) => `<th>${h}</th>`).join('') + '</tr></thead><tbody>' +
        order.filter((k) => V.by_verdict[k]).map((k) => {
          const v = V.by_verdict[k];
          return `<tr><td>${S['v_' + k]}</td><td class="num">${v.n}</td>` +
            `<td class="num">${v.hit_pct === null ? '—' : v.hit_pct + '%'}</td>` +
            `<td class="num">+${v.median_regret_pct.toFixed(1)}%</td>` +
            `<td>${S['vd_' + k]}</td></tr>`;
        }).join('') + '</tbody></table></div>' +
        `<p class="explain">${fill(S.b3_x, { since: V.since, ev: V.evaluable })}</p>`;
    }
    vc.innerHTML = `<p class="chart-title">${S.b3_t}</p>` +
                   `<p class="chart-note">${S.b3_n}</p>` + body;

    // 4枚目・5枚目: 予測した底値の日は当たったか、その日に買ったら得だったか
    const newGrid = () => { const g = document.createElement('div');
      g.className = 'grid-2'; host.appendChild(g); return g; };
    const newCard = (g) => { const c = document.createElement('div');
      c.className = 'card'; g.appendChild(c); return c; };
    const tile = (v, k) => `<div><div class="now">${v}</div>` +
      `<p class="chart-note">${k}</p></div>`;
    function timingWait(g, title) {
      newCard(g).innerHTML = `<p class="chart-title">${title}</p>` +
        `<p class="chart-note">${S.b4_n}</p>` +
        `<p class="explain">${fill(S.b4_wait, { since: V.since || '—' })}</p>`;
    }
    function timingTiles(g, T, title) {
      const tc = newCard(g);
      tc.innerHTML = `<p class="chart-title">${title}</p>` +
        `<p class="chart-note">${fill(S.b4_n2, { n: T.n, s: T.series })}</p>` +
        '<div class="tiles">' +
        tile(fill(S.b4_days, { d: T.day_abs_err_median }), S.b4_k1) +
        tile(T.within_pct + '%', fill(S.b4_k2, { t: T.tolerance_days })) +
        '</div>' +
        `<p class="explain">${fill(S.b4_x, {
          t: T.tolerance_days, e: T.early_pct, l: T.late_pct,
          q0: T.pred_dbd_q[0], q2: T.pred_dbd_q[2] })}</p>`;
    }
    function timingBars(g, T, title) {
      const R = T.regret;
      const bars = [
        { label: S.b5_a, r: R.app, tone: 's3' },
        { label: S.b5_n, r: R.now, tone: 's1' },
        { label: fill(S.b5_d, { d: T.rise_dbd }), r: R.deadline, tone: 's1' },
      ];
      const best = bars.reduce((a, b) => (b.r.mean < a.r.mean ? b : a), bars[0]);
      // 判定が期限より出発に近いと、期限の日の価格は判定の日の価格で代わる。
      const same = R.deadline.mean === R.now.mean && R.deadline.median === R.now.median;
      g.appendChild(columns(null, {
        title, note: S.b5_note, axis: '',
        fmt: (v, ax) => (v > 0 ? '+' : '') + (ax ? Math.round(v) : v.toFixed(1)) + '%',
        legend: [['s3', S.b5_a], ['s1', S.b5_l]],
        rows: bars.map((b) => ({ label: b.label, v: b.r.mean, tone: b.tone, tag: true,
          tip: `<b>${b.label}</b><br>` +
               `<span class="k">${S.b2_k2}</span><b>+${b.r.mean}%</b><br>` +
               `<span class="k">${S.b2_k1}</span><b>+${b.r.median}%</b><br>` +
               `<span class="k">${fill(S.b2_k3, { near: B.near_pct })}</span>` +
               `<b>${b.r.near}%</b>` })),
        explain: fill(S.b5_x, {
          a: R.app.mean, n: R.now.mean, d: R.deadline.mean, dd: T.rise_dbd,
          w: T.beat_deadline_pct,
          verdict: best === bars[0] ? S.b5_win : S.b5_lose }) +
          (same ? '<br>' + fill(S.b5_same, { dd: T.rise_dbd }) : ''),
      }));
    }
    const timed = dirs.filter((dr) => dr.timing);
    if (timed.length) {
      // 左右を方向でそろえる（上の図と同じ並び）。
      const g4 = newGrid();
      timed.forEach((dr) => {
        const title = fill(S.b_dir, { t: S.b4_t, dir: dirName(dr) });
        if (dr.timing.n) timingTiles(g4, dr.timing, title);
        else timingWait(g4, title);
      });
      if (timed.some((dr) => dr.timing.n)) {
        const g5 = newGrid();
        timed.forEach((dr) => {
          const title = fill(S.b_dir, { t: S.b5_t, dir: dirName(dr) });
          if (dr.timing.n) timingBars(g5, dr.timing, title);
          else newCard(g5).innerHTML = `<p class="chart-title">${title}</p>` +
            `<p class="explain">${fill(S.b4_wait, { since: V.since || '—' })}</p>`;
        });
      }
    } else {
      const T = B.timing || { n: 0 };
      const g2 = newGrid();
      if (!T.n) timingWait(g2, S.b4_t);
      else { timingTiles(g2, T, S.b4_t); timingBars(g2, T, S.b5_t); }
    }

    document.getElementById('buyCaveat').innerHTML =
      `<p>${fill(S.cav_b1, { bd0: B.departures[0], bd1: B.departures[1] })}</p>` +
      `<p>${fill(S.cav_b2, { obs: B.obs_start_median })}</p>`;
  })();

  /* ── 折れ線（1系列・面つき）──
     小さく並べるので、軸は最小限。終点だけ強調して「いまどこか」を示す。 */
  function spark(host, opts) {
    const pts = D.kpi.map((r, i) => ({ i, d: r.d, v: r[opts.key] }))
                     .filter((p) => p.v !== null && p.v !== undefined);
    const W = 300, H = 96, P = { t: 8, r: 10, b: 18, l: 30 };
    const vs = pts.map((p) => p.v);
    let lo = Math.min(...vs), hi = Math.max(...vs);
    if (opts.zero) lo = 0;
    if (hi === lo) hi = lo + 1;
    const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;
    if (opts.zero) lo = 0;
    const X = (i) => P.l + (i / (pts.length - 1)) * (W - P.l - P.r);
    const Y = (v) => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);

    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
      'aria-label': `${opts.label} の推移` });

    [0, .5, 1].forEach((f) => {
      const y = P.t + f * (H - P.t - P.b);
      svg.appendChild(el('line', { x1: P.l, x2: W - P.r, y1: y, y2: y, class: 'gridline' }));
    });
    [hi, lo].forEach((v, k) => {
      const t = el('text', { x: P.l - 5, y: P.t + k * (H - P.t - P.b) + 3,
        'text-anchor': 'end', class: 'axis-txt' });
      t.textContent = opts.fmt ? opts.fmt(v, true) : Math.round(v);
      svg.appendChild(t);
    });

    const line = pts.map((p, k) => `${k ? 'L' : 'M'}${X(p.i - pts[0].i)},${Y(p.v)}`).join(' ');
    svg.appendChild(el('path', { d: `${line} L${X(pts.length - 1)},${Y(lo)} L${X(0)},${Y(lo)} Z`,
      class: `ar-${opts.tone}` }));
    svg.appendChild(el('path', { d: line, fill: 'none', class: `ln-${opts.tone}`,
      'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));

    const last = pts[pts.length - 1];
    svg.appendChild(el('circle', { cx: X(pts.length - 1), cy: Y(last.v), r: 4.5,
      class: `fl-${opts.tone} pt`, 'stroke-width': 2 }));

    [pts[0], last].forEach((p, k) => {
      const t = el('text', { x: k ? W - P.r : P.l, y: H - 5,
        'text-anchor': k ? 'end' : 'start', class: 'axis-txt' });
      t.textContent = p.d; svg.appendChild(t);
    });

    const hit = el('rect', { x: P.l, y: 0, width: W - P.l - P.r, height: H,
      fill: 'transparent' });
    hit.addEventListener('pointermove', (e) => {
      const bb = svg.getBoundingClientRect();
      const rel = (e.clientX - bb.left) / bb.width * W;
      let best = pts[0], bd = 1e9;
      pts.forEach((p, k) => { const dd = Math.abs(X(k) - rel);
        if (dd < bd) { bd = dd; best = p; } });
      showTip(e, `<b>${best.d}</b><br><span class="k">${opts.label}</span>` +
        `<b>${opts.fmt ? opts.fmt(best.v) : best.v}</b>`);
    });
    hit.addEventListener('pointerleave', hideTip);
    svg.appendChild(hit);

    const prev = pts.length > 1 ? pts[pts.length - 2].v : last.v;
    const dv = last.v - prev;
    const cls = Math.abs(dv) < 1e-9 ? 'flat' : (opts.upIsBad === false
      ? (dv > 0 ? 'down' : 'up') : (dv > 0 ? 'up' : 'down'));
    const sign = dv > 0 ? '+' : '';
    host.innerHTML =
      `<p class="chart-title">${opts.label}</p>` +
      `<p class="chart-note">${opts.note}</p>` +
      // **「124 0」と並べない。** 値と差分が2つの数字に見えてしまうので、
      // 差分側にラベルを先に置く。
      `<div class="now">${opts.fmt ? opts.fmt(last.v) : last.v}` +
      `<span class="delta ${cls}">前日比 ${sign}${opts.fmt ? opts.fmt(dv) : dv}</span></div>`;
    host.appendChild(svg);
    if (opts.explain) {
      host.insertAdjacentHTML('beforeend',
        `<p class="explain">${opts.explain(pts)}</p>`);
    }
  }

  const pct = (v, ax) => (ax ? Math.round(v) + '%' : v.toFixed(1) + '%');
  const usage = document.getElementById('usage');
  [
    { key: 'total',    label: 'モニター総数',   note: '登録されている監視',
      tone: 's1', zero: true, upIsBad: false },
    { key: 'new',      label: '新規登録 / 日',  note: '24時間の作成数',
      tone: 's1', zero: true, upIsBad: false },
    { key: 'ret7',     label: '7日以内の再訪率', note: '一度でも開き直された割合',
      tone: 's2', zero: true, fmt: pct, upIsBad: false },
    { key: 'routes',   label: '稼働ルート',     note: '毎日価格を取りに行く本数',
      tone: 's2', zero: true, upIsBad: false },
  ].forEach((o) => { const c = document.createElement('div');
    c.className = 'card'; usage.appendChild(c); spark(c, o); });

  /* ── 精度（複数系列・配備の縦線つき）── */
  function multi(host, opts) {
    const rows = D.kpi.filter((r) => r[opts.series[0].key] !== null);
    // **1日も無いことがある。** 測りはじめる前の指標がこれ。ここで
    // 落ちると、以降のグラフごと描画が止まる（同じ script の中なので）。
    if (!rows.length) {
      host.innerHTML = `<p class="chart-title">${opts.label}</p>` +
                       `<p class="chart-note">${opts.note}</p>`;
      if (opts.explain) host.insertAdjacentHTML('beforeend',
        `<p class="explain">${opts.explain(rows)}</p>`);
      return;
    }
    const W = 320, H = 190, P = { t: 12, r: 14, b: 26, l: 38 };
    // **後から測りはじめた系列がある。** P90 は 2026-09-10 から。
    // 揃っていない日を捨てると、それ以前の履歴がまるごと消える。
    // 欠けている点は飛ばして、その系列だけ途中から描く。
    const has = (v) => v !== null && v !== undefined;
    const all = rows.flatMap((r) => opts.series.map((s) => r[s.key]))
                    .filter(has);
    let lo = Math.min(0, Math.min(...all)), hi = Math.max(...all);
    hi += (hi - lo) * 0.14;
    // **1日ぶんしか無い日がある。** 測りはじめた当日がそれ。
    // i/(n-1) は 0/0 になるので、真ん中に置く。
    const X = (i) => rows.length < 2 ? (P.l + W - P.r) / 2
                   : P.l + (i / (rows.length - 1)) * (W - P.l - P.r);
    const Y = (v) => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);

    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
      'aria-label': opts.label });

    const ticks = 4;
    for (let k = 0; k <= ticks; k++) {
      const v = lo + (hi - lo) * k / ticks, y = Y(v);
      svg.appendChild(el('line', { x1: P.l, x2: W - P.r, y1: y, y2: y, class: 'gridline' }));
      const t = el('text', { x: P.l - 6, y: y + 3, 'text-anchor': 'end', class: 'axis-txt' });
      t.textContent = Math.round(v) + '%'; svg.appendChild(t);
    }

    /* 曲線を引き直した日。**グラフの意味がここで変わる。** */
    const mi = rows.findIndex((r) => r.d === '08-24');
    if (mi >= 0) {
      svg.appendChild(el('line', { x1: X(mi), x2: X(mi), y1: P.t, y2: H - P.b,
        class: 'rule-v', 'stroke-dasharray': '3 3' }));
      const t = el('text', { x: X(mi) + 4, y: P.t + 9, class: 'axis-txt' });
      t.textContent = '曲線を引き直し'; svg.appendChild(t);
    }

    const labels = [];
    opts.series.forEach((s) => {
      const pts = rows.map((r, k) => [k, r[s.key]]).filter(([, v]) => has(v));
      if (!pts.length) return;
      const d = pts.map(([k, v], i) => `${i ? 'L' : 'M'}${X(k)},${Y(v)}`).join(' ');
      const line = { d, fill: 'none', class: `ln-${s.tone}`, 'stroke-width': 2,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round' };
      // **中央値と P90 は同じ色の実線と破線。** 色を分けると、どちらが
      // どの指標の裾なのかが読み取れない。同じ色で対にする。
      if (s.dash) line['stroke-dasharray'] = '4 3';
      svg.appendChild(el('path', line));
      pts.forEach(([k, v]) => svg.appendChild(el('circle', { cx: X(k), cy: Y(v),
        r: s.dash ? 2 : 3, class: `fl-${s.tone} pt`, 'stroke-width': 1.5 })));
      const [lk, lv] = pts[pts.length - 1];
      const lbl = el('text', { x: X(lk) + 6, y: Y(lv) + 3,
        class: `axis-txt fl-${s.tone}`, 'font-weight': 600 });
      lbl.textContent = lv.toFixed(1); svg.appendChild(lbl);
      labels.push(lbl);
    });

    // **値が近いと値札が重なって読めない。**（誤差 P90 55.5 と偏り P90
    // 52.5 が実測で重なった。）上から見て、近すぎるものを押し下げる。
    labels.sort((a, b) => +a.getAttribute('y') - +b.getAttribute('y'));
    labels.forEach((lbl, i) => {
      if (!i) return;
      const prev = +labels[i - 1].getAttribute('y');
      const y = +lbl.getAttribute('y');
      if (y - prev < 11) lbl.setAttribute('y', prev + 11);
    });

    // 1日ぶんしか無い日は、同じ日付を両端に出さない。真ん中に1つ。
    (rows.length < 2 ? [[rows[0], 'middle']]
                     : [[rows[0], 'start'], [rows[rows.length - 1], 'end']]
    ).forEach(([r, anchor]) => {
      const t = el('text', { y: H - 8, 'text-anchor': anchor, class: 'axis-txt',
        x: anchor === 'middle' ? (P.l + W - P.r) / 2
         : anchor === 'end' ? W - P.r : P.l });
      t.textContent = r.d; svg.appendChild(t);
    });

    const cross = el('line', { y1: P.t, y2: H - P.b, class: 'rule-v', opacity: 0 });
    svg.appendChild(cross);
    const hit = el('rect', { x: P.l, y: 0, width: W - P.l - P.r, height: H,
      fill: 'transparent' });
    hit.addEventListener('pointermove', (e) => {
      const bb = svg.getBoundingClientRect();
      const rel = (e.clientX - bb.left) / bb.width * W;
      let bi = 0, bd = 1e9;
      rows.forEach((r, k) => { const dd = Math.abs(X(k) - rel);
        if (dd < bd) { bd = dd; bi = k; } });
      cross.setAttribute('x1', X(bi)); cross.setAttribute('x2', X(bi));
      cross.setAttribute('opacity', 1);
      showTip(e, `<b>${rows[bi].d}</b><br>` + opts.series.map((s) => {
        const v = rows[bi][s.key];
        return `<span class="k">${s.name}</span>`
             + `<b>${has(v) ? v.toFixed(1) + '%' : '—'}</b>`;
      }).join('<br>'));
    });
    hit.addEventListener('pointerleave', () => { hideTip(); cross.setAttribute('opacity', 0); });
    svg.appendChild(hit);

    host.innerHTML = `<p class="chart-title">${opts.label}</p>` +
                     `<p class="chart-note">${opts.note}</p>`;
    host.appendChild(svg);
    host.insertAdjacentHTML('beforeend', '<div class="legend">' +
      opts.series.map((s) => `<span><i class="swatch sw-${s.tone}` +
        `${s.dash ? ' dash' : ''}"></i>${s.name}</span>`).join('') + '</div>');
    // **グラフの読み方をその場に置く。** 別の場所に書くと、数字を見て
    // いる人の目に入らない。文中の値は当日のデータから作る（固定文に
    // すると、いつか嘘になる）。
    if (opts.explain) {
      host.insertAdjacentHTML('beforeend',
        `<p class="explain">${opts.explain(rows)}</p>`);
    }
  }

  const acc = document.getElementById('accuracy');
  [
    { label: '誤差と偏り（出発30日以内）',
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
      ] },
  ].forEach((o) => { const c = document.createElement('div');
    c.className = 'card'; acc.appendChild(c); multi(c, o); });

  /* ── 下落トップ10（横棒・単一色。長さが量を表すので色は分けない）── */
  (function () {
    const rows = D.drops;
    const RH = 26, W = 640, P = { t: 6, r: 96, b: 22, l: 118 };
    const H = P.t + rows.length * RH + P.b;
    const worst = -70;
    const X = (v) => P.l + (v / worst) * (W - P.l - P.r);

    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
      'aria-label': '開始時からの最大下落率 上位10路線' });

    for (let v = 0; v >= worst; v -= 10) {
      svg.appendChild(el('line', { x1: X(v), x2: X(v), y1: P.t, y2: H - P.b,
        class: 'gridline' }));
      const t = el('text', { x: X(v), y: H - 8, 'text-anchor': 'middle', class: 'axis-txt' });
      t.textContent = v + '%'; svg.appendChild(t);
    }

    rows.forEach((r, k) => {
      const y = P.t + k * RH + 5, h = RH - 12;
      const lbl = el('text', { x: P.l - 10, y: y + h - 2, 'text-anchor': 'end',
        class: 'axis-txt ink', 'font-weight': 600 });
      lbl.textContent = r.leg; svg.appendChild(lbl);
      const sub = el('text', { x: P.l - 10, y: y + h + 8, 'text-anchor': 'end',
        class: 'axis-txt' });
      sub.textContent = r.dep.slice(5) + ' 発'; svg.appendChild(sub);

      const bar = el('rect', { x: P.l, y, width: Math.max(X(r.drop_pct) - P.l, 2),
        height: h, rx: 3, class: 'fl-s1' });
      bar.addEventListener('pointermove', (e) => showTip(e,
        `<b>${r.leg}</b> ${r.dep} 発<br>` +
        `<span class="k">開始</span><b>$${r.start.toLocaleString()}</b>` +
        ` <span class="k">→ 最安</span><b>$${r.low.toLocaleString()}</b><br>` +
        `<span class="k">下落</span><b>${r.drop_pct}%</b>` +
        ` <span class="k">最安は出発</span><b>${r.low_dbd}日前</b>`));
      bar.addEventListener('pointerleave', hideTip);
      svg.appendChild(bar);

      const val = el('text', { x: X(r.drop_pct) + 8, y: y + h - 2,
        class: 'axis-txt ink', 'font-weight': 600 });
      val.textContent = r.drop_pct + '%'; svg.appendChild(val);
    });
    document.getElementById('drops').appendChild(svg);

    const tb = document.querySelector('#dropTable tbody');
    tb.innerHTML = rows.map((r) => {
      const cur = r.cur_pct, cls = cur < -0.5 ? 'down' : cur > 0.5 ? 'up' : 'flat';
      return `<tr><td class="leg">${r.leg}</td><td class="num">${r.dep}</td>
        <td class="num">${r.n}日</td><td class="num">$${r.start.toLocaleString()}</td>
        <td class="num">$${r.low.toLocaleString()}</td>
        <td class="num" style="color:var(--good);font-weight:600">${r.drop_pct}%</td>
        <td class="num">出発${r.low_dbd}日前</td>
        <td class="num">$${r.cur.toLocaleString()}</td>
        <td class="num ${cls}">${cur > 0 ? '+' : ''}${cur}%</td></tr>`;
    }).join('');
  })();

  /* ── 元データ表 ── */
  document.querySelector('#rawTable tbody').innerHTML = D.kpi.map((r) => {
    const n = (v, s) => v === null || v === undefined ? '—' : v + (s || '');
    return `<tr><td class="num">${r.d}</td><td class="num">${r.total}</td>
      <td class="num">${r.new}</td><td class="num">${r.creators}</td>
      <td class="num">${n(r.ret7, '%')}</td><td class="num">${r.routes}</td>
      <td class="num">${r.recs.toLocaleString()}</td><td class="num">${r.errs}</td>
      <td class="num">${n(r.aerr, '%')}</td><td class="num">${n(r.abias, '%')}</td>
      <td class="num">${n(r.aband, '%')}</td></tr>`;
  }).join('');
