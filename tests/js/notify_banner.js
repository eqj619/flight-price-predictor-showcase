/**
 * 一覧の上に出る通知の誘い（課題4の補助）を、index.js をそのまま
 * 走らせて確かめる。
 *
 * 見ているのは2つ。
 *
 * 1. **送らない通知を誘っていないか。** 往復・路線が未確定・値上がり
 *    開始日を過ぎた、のどれかなら notify.py は送らない
 * 2. **断りが画面をまたいで効くか。** ここで断った人にモニター画面が
 *    また尋ねるなら、断る意味が無い
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const root = path.resolve(__dirname, '..', '..');

function node(tag = 'div') {
  const el = {
    tagName: tag, children: [], listeners: {}, style: {}, dataset: {},
    hidden: false, disabled: false, value: '', className: '', href: '',
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    querySelector(sel) { return ((this._q ||= {})[sel] ||= node()); },
    setAttribute() {}, select() {}, focus() {}, remove() {},
  };
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

const RISE = { date: '2026-10-17', days: 39,
               days_before_departure: 52, penalty_pct: 17 };

function watchOf(token, over = {}, riseOver = {}) {
  return Object.assign({
    token, origin: 'SAN', destination: 'HND',
    departure_date: '2026-12-08', return_date: null,
    routes: [{
      origin: 'SAN', destination: 'HND', nearby: false,
      summary: { basis: 'observed', observed_days: 12 },
      recommendation: { verdict: 'wait',
                        price_rises_after: Object.assign({}, RISE, riseOver) },
    }],
  }, over);
}

const store = {};
global.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};

async function run({ watches, enabled = true, supported = true,
                     already = [], dismissed = false }) {
  for (const k of Object.keys(store)) delete store[k];
  if (dismissed) store.fppNotifyOfferDismissed = '1';

  const ids = {};
  global.document = {
    getElementById: (id) => (ids[id] ||= node()),
    createElement: (tag) => node(tag),
    addEventListener() {},
  };
  global.location = { pathname: '/', origin: 'https://example.test' };
  global.I18N = {
    init() {}, lang: 'ja',
    t: (k, p) => (p ? `${k}(${Object.entries(p).map(([a, b]) => `${a}=${b}`).join(',')})` : k),
  };
  global.Airports = { attach() {} };
  global.Store = {
    list: () => watches.map((w) => ({ token: w.token })),
    add() {}, remove() {}, collection: () => null, setCollection() {},
  };
  global.Api = {
    async getWatch(t) { return watches.find((w) => w.token === t) || null; },
    async getCollection() { return null; },
    async removeFromCollection() {}, async addToCollection() {},
    async createCollection() { return { token: 'c' }; },
    async createWatch() { throw new Error('未使用'); },
  };
  global.Push = {
    supported,
    async prime() { return { enabled }; },
    async subscribedWatches() { return already; },
    offerDismissed: () => !!localStorage.getItem('fppNotifyOfferDismissed'),
    dismissOffer: () => localStorage.setItem('fppNotifyOfferDismissed', '1'),
  };

  try {
    eval(fs.readFileSync(path.join(root, 'static', 'js', 'index.js'), 'utf8'));
  } catch (err) { console.error('index.js が落ちた:', err); throw err; }
  for (let i = 0; i < 60; i++) await new Promise((r) => setImmediate(r));
  return ids.notifyBanner;
}

(async () => {
  // ── 出るとき。数えるのは「送れるモニター」だけ ────────────────
  const banner = await run({ watches: [
    watchOf('a'),                                        // 送れる
    watchOf('b'),                                        // 送れる
    watchOf('c', { return_date: '2026-12-20' }),         // 往復
    watchOf('d', {}, { days: -3 }),                      // 開始日を過ぎた
    watchOf('e', { routes: [{ nearby: false,
      summary: { basis: 'unconfirmed' },
      recommendation: { price_rises_after: RISE } }] }), // 路線が未確定
  ] });
  assert.ok(!banner.hidden, '送れるモニターがあれば帯が出ること');
  const head = banner.children[0].children[0];
  assert.strictEqual(head.textContent, 'banner.head(n=2)',
    '数えるのは送れるモニターだけ（往復・期限切れ・未確定は入れない）');

  // **許可はここで求めず、文面を見せられる画面へ送る。**
  const link = banner.children[0].children[2].children[0];
  assert.strictEqual(link.href, '/w/a', '該当するモニターへ送ること');

  // ── 断りは画面をまたいで効く ──────────────────────────────
  const dismiss = banner.children[0].children[2].children[1];
  dismiss.listeners.click[0]();
  assert.ok(banner.hidden, '断ったらその場で消えること');
  assert.strictEqual(localStorage.getItem('fppNotifyOfferDismissed'), '1',
    'モニター画面の誘いと同じ鍵に覚えること（別々だと二度尋ねる）');

  // ── 出てはいけないとき ────────────────────────────────────
  const quiet = {
    '送れるモニターが無い': { watches: [watchOf('c', { return_date: '2026-12-20' })] },
    'すべて購読済み': { watches: [watchOf('a')], already: ['a'] },
    '既に断られている': { watches: [watchOf('a')], dismissed: true },
    'サーバ側で通知が無効': { watches: [watchOf('a')], enabled: false },
    'ブラウザが非対応': { watches: [watchOf('a')], supported: false },
    'モニターが1件も無い': { watches: [] },
  };
  for (const [why, args] of Object.entries(quiet)) {
    const b = await run(args);
    assert.ok(b.hidden && !b.children.length, `${why}: 帯を出さないこと`);
  }

  console.log('notify_banner: 通過');
})().catch((err) => { console.error(err); process.exit(1); });
