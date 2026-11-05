/**
 * 通知の誘いが「出るべきときに出て、出てはいけないときに出ない」ことを
 * 確かめる。**watch.js をそのまま走らせる。**
 *
 * なぜ要るか。2026-08-29 に価格トレンドのタブへ誘いを置いたが、10日間で
 * 購読は3件のまま動かなかった。原因は場所で、**125件中84件（67%）は
 * 作った日以外に一度も開かれていない**（2026-09-08 実測）。誘いを最初の
 * 画面へ移したのがこの変更なので、**条件を1つ取り違えると誘いが二度と
 * 出ない**。構文検査では捕まらない。
 *
 * 特に見ているのは、**送らない通知を誘っていないか**。往復・路線が
 * 未確定・値上がり開始日を過ぎた、のどれかなら notify.py は送らない。
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const root = path.resolve(__dirname, '..', '..');

function node(tag = 'div') {
  const el = new Node();
  Object.assign(el, {
    tagName: tag, children: [], listeners: {}, style: {}, dataset: {},
    hidden: false, disabled: false, checked: false, value: '', className: '',
    options: [], selectedIndex: 0,
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    classList: { toggle() {}, add() {}, remove() {} },
    querySelector(sel) { return ((this._q ||= {})[sel] ||= node()); },
    querySelectorAll() { return []; },
    closest() { return null; },
    remove() { this._removed = true; },
    replaceChildren(...cs) { this.children.length = 0; this.children.push(...cs); },
    insertBefore(c) { this.children.unshift(c); return c; },
    setAttribute() {}, removeAttribute() {}, select() {}, focus() {},
    getContext: () => ({}),
  });
  let text = '';
  Object.defineProperty(el, 'textContent', {
    get() { return text; },
    set(v) { text = String(v); this.children.length = 0; },
  });
  Object.defineProperty(el, 'innerHTML', {
    get() { return ''; }, set() { this.children.length = 0; },
  });
  return el;
}

/** 節点の下にある文字を全部つなげる。何が書かれたかを読むため。 */
function textOf(el) {
  return (el.textContent || '') + el.children.map(textOf).join(' ');
}
function buttons(el, found = []) {
  if (el.tagName === 'button') found.push(el);
  el.children.forEach((c) => buttons(c, found));
  return found;
}

// watch.js は `content instanceof Node` で節点か文字かを見分ける。
// 偽の節点をこの型から作れば、その判定がそのまま通る。
class Node {}
global.Node = Node;

const store = {};
global.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};

const RISE = { date: '2026-10-17', days: 39,
               days_before_departure: 52, penalty_pct: 17 };

function makeWatch(over = {}) {
  return Object.assign({
    token: 'tok', origin: 'SAN', destination: 'HND',
    departure_date: '2026-12-08', return_date: null,
    created_at: '2026-09-08T00:00:00Z',
    routes: [{
      origin: 'SAN', destination: 'HND', nearby: false,
      summary: { basis: 'observed', observed_days: 12 },
      series: [{ stops: 0, via_airport: null, airline: 'ANA',
                      basis: 'observed', observed_days: 12,
                      observed: [{ check_date: '2026-09-07', price_usd: 900 }],
                      projected: [] }],
      recommendation: { verdict: 'wait', price_rises_after: RISE },
    }],
  }, over);
}

/** watch.js を1回丸ごと走らせて、誘いの箱を返す。 */
async function run({ watch, subscribed = false, enabled = true,
                     supported = true, dismissed = false }) {
  for (const k of Object.keys(store)) delete store[k];
  if (dismissed) store.fppNotifyOfferDismissed = '1';

  const ids = {};
  global.document = {
    getElementById: (id) => (ids[id] ||= node()),
    createElement: (tag) => node(tag),
    querySelectorAll: () => [],
    addEventListener() {},
  };
  global.window = { location: { pathname: '/w/tok' }, CHANGELOG: [],
                    confirm: () => true };
  global.location = global.window.location;
  global.requestAnimationFrame = (fn) => fn();
  global.I18N = {
    init() {}, lang: 'ja',
    // 引数を残して返す。文面の差し込みが効いているかを読むため。
    t: (k, p) => (p ? `${k}(${Object.entries(p).map(([a, b]) => `${a}=${b}`).join(',')})` : k),
  };
  global.Store = { list: () => [], add() {}, remove() {} };
  global.Api = {
    async getWatch() { return watch; },
    async updateWatch() { return watch; },
    async deleteWatch() {},
  };
  const subscribeCalls = [];
  global.Push = {
    supported, isIOSNonSafari: false, needsHomeScreen: false,
    async prime() { return { enabled }; },
    async isSubscribed() { return subscribed; },
    async subscribe(t) { subscribeCalls.push(t); },
    async unsubscribe() {},
    // 断りの記憶は本物と同じ鍵で。**画面をまたいで1つ**であることが
    // 効いているかを、一覧側の試験（remove_from_list ではなく notify_banner）
    // と合わせて見る。
    offerDismissed: () => !!localStorage.getItem('fppNotifyOfferDismissed'),
    dismissOffer: () => localStorage.setItem('fppNotifyOfferDismissed', '1'),
  };
  global.PriceChart = { render() {} };
  global.Chart = class { constructor() { this.destroy = () => {}; } static register() {} };

  eval(fs.readFileSync(path.join(root, 'static', 'js', 'watch.js'), 'utf8'));
  for (let i = 0; i < 60; i++) await new Promise((r) => setImmediate(r));
  return { box: ids.notifyOffer, ids, subscribeCalls };
}

(async () => {
  // ── 出るとき ────────────────────────────────────────────────
  const { box, subscribeCalls } = await run({ watch: makeWatch() });
  assert.ok(!box.hidden && box.children.length,
            '片道・開始日が未来なら誘いが出ること');

  const text = textOf(box);
  // **届く文面そのものを見せているか。** 何が来るか分からないものは
  // 許可しにくい。ここが抜けると誘いはただの「許可して」になる。
  assert.ok(text.includes('offer.sample.title'), '通知の題を見せること');
  assert.ok(text.includes('offer.sample.body'), '通知の本文を見せること');
  assert.ok(text.includes('days=52'),
            '残日数は送信時の値（開始日当日＝出発52日前）を出すこと');
  assert.ok(text.includes('pct=17'), '上昇率はサーバの実測値を出すこと');
  assert.ok(text.includes('offer.fine'), '1回だけ・解除できることを書くこと');

  const [yes, no] = buttons(box);
  assert.ok(yes && no, '「知らせる」と「いいえ」が出ること');

  // ── 押して初めて許可を求める ──────────────────────────────
  assert.deepStrictEqual(subscribeCalls, [],
    '**読み込みだけで許可を求めないこと。** 自動で出すと断られ、'
    + 'ブラウザは二度と出し直させてくれない');
  await yes.listeners.click[0]();
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
  assert.deepStrictEqual(subscribeCalls, ['tok'], '押されたら購読すること');

  // ── 断ったら二度と出さない ────────────────────────────────
  const declined = await run({ watch: makeWatch() });
  const [, noBtn] = buttons(declined.box);
  noBtn.listeners.click[0]();
  assert.ok(declined.box.hidden, '断ったらその場で消えること');
  assert.strictEqual(localStorage.getItem('fppNotifyOfferDismissed'), '1',
                     '断ったことを覚えること');

  // ── 出てはいけないとき ────────────────────────────────────
  const quiet = {
    '既に断られている': { watch: makeWatch(), dismissed: true },
    '既に購読している': { watch: makeWatch(), subscribed: true },
    'サーバ側で通知が無効': { watch: makeWatch(), enabled: false },
    'ブラウザが非対応': { watch: makeWatch(), supported: false },
    // 送信側が送らないもの。誘えば嘘になる。
    '往復': { watch: makeWatch({ return_date: '2026-12-20' }) },
    '開始日を過ぎている': { watch: makeWatch({
      routes: [Object.assign(makeWatch().routes[0], {
        recommendation: { price_rises_after: Object.assign({}, RISE, { days: -3 }) },
      })] }) },
    '出発済み（開始日なし）': { watch: makeWatch({
      routes: [Object.assign(makeWatch().routes[0], {
        recommendation: { price_rises_after: null } })] }) },
    '路線が未確定': { watch: makeWatch({
      routes: [Object.assign(makeWatch().routes[0], {
        summary: { basis: 'unconfirmed', observed_days: 0 },
        series: [{ stops: 0, via_airport: null, airline: '',
                        basis: 'unconfirmed', observed_days: 0,
                        observed: [], projected: [] }] })] }) },
    '便が見つからない': { watch: makeWatch({
      routes: [Object.assign(makeWatch().routes[0], {
        summary: { basis: 'no_flights', observed_days: 0 },
        series: [{ stops: 0, via_airport: null, airline: '',
                        basis: 'no_flights', observed_days: 0,
                        observed: [], projected: [] }] })] }) },
  };
  for (const [why, args] of Object.entries(quiet)) {
    const r = await run(args);
    assert.ok(r.box.hidden && !r.box.children.length, `${why}: 誘いを出さないこと`);
  }

  console.log('notify_offer: 通過');
})().catch((err) => { console.error(err); process.exit(1); });
