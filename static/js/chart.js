/**
 * 価格グラフ。実測・予測・信頼帯を1枚に描く。
 *
 * **帯を潰さないこと。** 予測を1本の線で見せると、実測3日分でも40日分でも
 * 同じ確信度に見える。帯の広さがそのまま不確かさの表示になっている。
 */
// ダークテーマ用の軸・凡例の色。style.css の --text-secondary に合わせる。
const TICK = '#94a3b8';
const GRID = 'rgba(255, 255, 255, 0.06)';
// 「待つ/上昇」と同じ橙。色の意味を画面間で揃える。
// **緑（買い時）は使わない。** 2026-08-15 の実測で「待てば安くなる帯」は
// 無いと分かった。この印は安さではなく期限を表す。
const RISING = '#f59e0b';
// 背景に重ねる他の便の上限。多すぎると主役が埋もれる。
const MAX_BACKGROUND = 8;

/**
 * **値上がりが始まる日から先**を縦の帯で敷くプラグイン。
 *
 * 2026-08-15 まではここに「底値帯」を敷いていた。実測（212系列 / 13,004点）
 * で出発52日前より前がほぼ平坦だと分かったので、意味を反転させた。
 * 緑で「ここが安い」と示すと待たせてしまう。**橙で「ここから上がる」**を示す。
 *
 * Chart.js の annotation プラグインは同梱していないので自前で描く。
 * **データセットより先に描く**（beforeDatasetsDraw）。後から描くと帯が
 * 線と信頼帯を覆い、価格そのものが読めなくなる。
 */
const risingBand = {
  id: 'risingBand',
  beforeDatasetsDraw(chart, _args, opts) {
    const from = opts && opts.from;
    if (!from) return;
    const x = chart.scales.x;
    const labels = chart.data.labels;
    // 期限が描画範囲より前にあることがある（すでに上昇局面）。その場合は
    // 左端から敷く。範囲より後なら描かない。
    let start = labels.findIndex((d) => d >= from);
    if (start < 0) return;                    // 期限が右端より後
    if (labels[0] >= from) start = 0;         // すでに上昇局面

    const left = x.getPixelForValue(start);
    const { top, bottom, right } = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.fillStyle = 'rgba(245, 158, 11, 0.12)';
    ctx.fillRect(left, top, Math.max(right - left, 2), bottom - top);
    // 境界の線は始点だけ。終点は出発日（グラフの右端）なので引かない。
    if (start > 0) {
      ctx.strokeStyle = 'rgba(245, 158, 11, 0.65)';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(left, top);
      ctx.lineTo(left, bottom);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (opts.label) {
      ctx.fillStyle = RISING;
      ctx.font = '600 11px system-ui, sans-serif';
      // 帯が狭いと中央揃えでは外へはみ出す。左端から書き起こす。
      ctx.textAlign = 'left';
      ctx.fillText(opts.label, Math.min(left + 6, right - 90), top + 13);
    }
    ctx.restore();
  },
};

/**
 * 便を一意に指す文字列。
 *
 * **出発時刻まで含める。** 同じ社・同じ経由地でも1日に何本も飛ぶ
 * （SAN→HND の United+ANA・LAX 経由は 06:15 と 17:00 の2本）。3つで
 * 指していた頃は、別の便が同じ鍵になって1本の線に見えていた。
 */
function keyOf(itinerary) {
  return `${itinerary.stops}|${itinerary.via_airport || ''}|${itinerary.airline}`
    + `|${itinerary.departure_time || ''}`;
}

/** 日付 -> 点 の対応表にする。 */
function byDate(itinerary, field) {
  const out = new Map();
  for (const point of itinerary[field] || []) out.set(point.check_date, point);
  return out;
}

const PriceChart = {
  /**
   * 1便を主役に描く。
   *
   * **以前は各日の最安に畳んでいたが、それは価格の推移ではない。**
   * 最安の便は日によって入れ替わるので、畳んだ線には便の差し替えが
   * 混ざる。実測（同一便を翌日と突き合わせた1,002組）では、便を固定した
   * 日次変化が平均 1.61% なのに対し、ルート最安で見ると 5.27% と 3.3倍に
   * なる。差の大半は価格が動いたのではなく、別の便に変わっただけ。
   *
   * 利用者が知りたいのは「自分が買う便が上がるのか下がるのか」なので、
   * 便を固定して追う。他の便は薄い線で背景に置き、水準の広がりが
   * 分かるようにする。
   */
  render(canvas, series, risesAfter, selectedKey, jpyRate) {
    const list = series || [];
    const focus = list.find((s) => keyOf(s) === selectedKey) || list[0] || null;
    const others = list.filter((s) => s !== focus).slice(0, MAX_BACKGROUND);

    const observed = focus ? byDate(focus, 'observed') : new Map();
    const projected = focus ? byDate(focus, 'projected') : new Map();
    // この便の実測がゼロなら、線は推定。凡例と色で言い分ける。
    const modelOnly = !focus || focus.basis !== 'observed';

    // 横軸は全便の和集合。主役だけにすると、背景の線が途中で切れる。
    const labels = [...new Set(list.flatMap((s) => [
      ...(s.observed || []).map((p) => p.check_date),
      ...(s.projected || []).map((p) => p.check_date),
    ]))].sort();
    const at = (map, key, field) => {
      const point = map.get(key);
      return point ? point[field] : null;
    };

    // 実測も推定も無い（履歴だけ）のときは、中身の無い凡例を並べない。
    // 「実測」「モデル推定」と出ているのに線が無いと、壊れて見える。
    const datasets = [
      {
        label: I18N.t('chart.band'),
        data: labels.map((d) => at(projected, d, 'upper_usd')),
        borderWidth: 0,
        pointRadius: 0,
        backgroundColor: modelOnly
          ? 'rgba(148, 163, 184, 0.16)' : 'rgba(99, 102, 241, 0.20)',
        fill: '+1',
        spanGaps: true,
      },
      {
        label: '',
        data: labels.map((d) => at(projected, d, 'lower_usd')),
        borderWidth: 0,
        pointRadius: 0,
        fill: false,
        spanGaps: true,
      },
      {
        label: I18N.t(modelOnly ? 'chart.estimate' : 'chart.projected'),
        data: labels.map((d) => at(projected, d, 'price_usd')),
        borderColor: modelOnly ? '#94a3b8' : '#818cf8',
        borderDash: [6, 4],
        borderWidth: 2,
        pointRadius: 0,
        fill: false,
        spanGaps: true,
      },
      {
        label: I18N.t('chart.observed'),
        data: labels.map((d) => at(observed, d, 'price_usd')),
        borderColor: '#34d399',
        backgroundColor: '#34d399',
        borderWidth: 2,
        pointRadius: 3,
        fill: false,
        // **ここは繋がない。** 便が出てこなかった日は「売っていなかった」で
        // あって、価格が線形に動いたわけではない。実測では同じ便が翌日も
        // 出てくる割合は 84%。
        spanGaps: false,
      },
    ];

    // 他の便を薄く重ねる。凡例には出さない（主役が埋もれる）。
    for (const other of others) {
      const points = byDate(other, 'observed');
      const source = points.size ? points : byDate(other, 'projected');
      datasets.push({
        label: '',
        data: labels.map((d) => at(source, d, 'price_usd')),
        borderColor: 'rgba(148, 163, 184, 0.28)',
        borderWidth: 1,
        pointRadius: 0,
        fill: false,
        spanGaps: false,
        order: 10,
      });
    }

    if (canvas._chart) canvas._chart.destroy();
    canvas._chart = new Chart(canvas, {
      type: 'line',
      data: { labels, datasets },
      plugins: [risingBand],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          y: {
            // 軸の値も換算する。**data はドルのまま**にしておき、表示の
            // 直前だけ変換する（データとモデルは通貨を持たない）。
            ticks: {
              callback: (v) => (jpyRate
                ? `¥${Math.round(v * jpyRate / 1000).toLocaleString()}k`
                : `US$${v}`),
              color: TICK,
            },
            grid: { color: GRID },
          },
          x: {
            ticks: { maxTicksLimit: 10, color: TICK },
            grid: { color: GRID },
          },
        },
        plugins: {
          risingBand: {
            from: risesAfter ? risesAfter.date : null,
            label: risesAfter ? I18N.t('chart.rising') : '',
          },
          legend: {
            labels: { filter: (item) => item.text !== '', color: TICK },
          },
          tooltip: {
            callbacks: {
              label: (ctx) => {
                if (!ctx.dataset.label) return null;
                const v = ctx.parsed.y;
                const shown = jpyRate
                  ? `¥${(Math.round(v * jpyRate / 100) * 100).toLocaleString()}`
                  : `US$${Math.round(v)}`;
                return `${ctx.dataset.label}: ${shown}`;
              },
            },
          },
        },
      },
    });
  },
};

/** 日付ごとに、全便の中で最安の点を返す。 */
function cheapestByDate(series, field) {
  const out = new Map();
  for (const itinerary of series) {
    for (const point of itinerary[field] || []) {
      const current = out.get(point.check_date);
      if (!current || point.price_usd < current.price_usd) {
        out.set(point.check_date, point);
      }
    }
  }
  return out;
}

window.PriceChart = PriceChart;
