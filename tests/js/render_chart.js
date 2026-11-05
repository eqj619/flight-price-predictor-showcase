/**
 * chart.js を素の Node で実行し、描画が例外を投げないことを確かめる。
 *
 * **`node --check` では防げない種類の壊れ方があるために要る。** 未定義の
 * 識別子（消し忘れた関数の呼び出しなど）は構文エラーにならないので、
 * 構文検査は通ってしまう。2026-08-15 に実際これで本番のグラフが空になった
 * （帯のプラグインを差し替えたとき keyOf と byDate を巻き添えで消した）。
 *
 * Chart.js 本体は読み込まない。**知りたいのは「render が最後まで走るか」**
 * であって、描画結果の見た目ではない。設定オブジェクトの組み立てまでを
 * 通せば、識別子の抜けは必ず露見する。
 */
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..', '..');

global.window = {};
global.I18N = { t: (key) => key };
// 設定を受け取るだけの偽 Chart。プラグインの draw までは呼ばれない。
global.Chart = class {
  constructor(_ctx, config) { this.config = config; this.destroy = () => {}; }
  static register() {}
};

eval(fs.readFileSync(path.join(root, 'static', 'js', 'chart.js'), 'utf8'));

const canvas = { getContext: () => ({}) };
const series = [
  {
    stops: 1, via_airport: 'ICN', airline: 'Asiana Airlines, Korean Air',
    basis: 'observed', observed_days: 3,
    observed: [{ check_date: '2026-08-13', price_usd: 776 },
               { check_date: '2026-08-14', price_usd: 780 }],
    projected: [{ check_date: '2026-08-15', price_usd: 782,
                  lower_usd: 700, upper_usd: 860 }],
  },
  {
    stops: 0, via_airport: null, airline: 'ANA',
    basis: 'observed', observed_days: 2,
    observed: [{ check_date: '2026-08-14', price_usd: 1180 }],
    projected: [],
  },
];
const rise = { date: '2026-10-17', days: 63,
               days_before_departure: 52, penalty_pct: 17 };

// 呼び方を変えて通す。どれか1つでも落ちれば失敗。
const cases = [
  ['通常', () => window.PriceChart.render(canvas, series, rise, null, null)],
  ['便を選択', () => window.PriceChart.render(canvas, series, rise,
    '1|ICN|Asiana Airlines, Korean Air', null)],
  ['期限なし（出発済み）', () => window.PriceChart.render(canvas, series, null, null, null)],
  ['円表示', () => window.PriceChart.render(canvas, series, rise, null, 159.01)],
  ['系列ゼロ', () => window.PriceChart.render(canvas, [], rise, null, null)],
  ['実測ゼロ（推定のみ）', () => window.PriceChart.render(canvas,
    [{ stops: 0, via_airport: null, airline: '', basis: 'simulated',
       observed: [], observed_days: 0,
       projected: [{ check_date: '2026-08-15', price_usd: 800,
                     lower_usd: 700, upper_usd: 900 }] }], rise, null, null)],
];

let failed = 0;
for (const [name, run] of cases) {
  try {
    run();
    console.log(`ok   ${name}`);
  } catch (e) {
    console.log(`FAIL ${name}: ${e.constructor.name} - ${e.message}`);
    failed += 1;
  }
}
process.exit(failed ? 1 : 0);
