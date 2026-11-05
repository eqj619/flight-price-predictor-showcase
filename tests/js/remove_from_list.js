/**
 * 「一覧から外す」が本当に外れることを確かめる。
 *
 * **2026-08-29 の不具合。** ボタンを押しても一覧からモニターが消えなかった。
 * 押した直後は消えるが、renderList が一覧トークンを見てサーバ側を正とし、
 * localStorage を上書きしたうえ Store.add で書き戻していた。
 *
 *     forget click → Store.remove（localStorage だけ）
 *                  → renderList → Api.getCollection（サーバは消していない）
 *                  → items = collection.watches   ← 戻ってくる
 *                  → items.forEach(Store.add)     ← 消したものを書き戻す
 *
 * `node --check` も既存の試験も、この壊れ方は見ない。**画面が要る種類の
 * 不具合なので、画面のかわりに最小の DOM を置いて index.js をそのまま
 * 走らせる。**
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const root = path.resolve(__dirname, '..', '..');

// ── 最小の DOM ───────────────────────────────────────────────
function node(tag = 'div') {
  const el = {
    tagName: tag, children: [], listeners: {}, style: {}, dataset: {},
    hidden: false, disabled: false, value: '', className: '',
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    // セレクタごとに同じ節点を返す。index.js は #tripType や
    // input[name=...] を掴んで listener を付けるので null では落ちる。
    querySelector(sel) { return ((this._q ||= {})[sel] ||= node()); },
    select() {}, focus() {}, remove() {},
  };
  // **textContent は素の属性ではない。** 実装は listEl.textContent = ''
  // で一覧を空にしている。素の代入にすると子が残り、再描画のたびに
  // 積み上がって「消えない」を取り違える。
  let text = '';
  Object.defineProperty(el, 'textContent', {
    get() { return text; },
    set(v) { text = String(v); this.children.length = 0; },
  });
  Object.defineProperty(el, 'innerHTML', {
    get() { return ''; },
    set() { this.children.length = 0; },
  });
  return el;
}

const ids = {};
const byId = (id) => (ids[id] ||= node());

global.document = {
  getElementById: byId,
  createElement: (tag) => node(tag),
  addEventListener() {},
};
global.location = { pathname: '/', origin: 'https://example.test' };
global.I18N = { init() {}, t: (k) => k, lang: 'ja' };
global.Airports = { attach() {} };

// ── 中身のある偽サーバ ───────────────────────────────────────
const COLLECTION = 'coll-token';
const watches = {
  'w1': { token: 'w1', origin: 'NRT', destination: 'SAN',
          departure_date: '2026-09-01', return_date: null, routes: [] },
  'w2': { token: 'w2', origin: 'SAN', destination: 'HND',
          departure_date: '2026-11-19', return_date: '2026-12-11', routes: [] },
};
let serverList = ['w1', 'w2'];
const calls = [];

global.Store = {
  _items: [{ token: 'w1' }, { token: 'w2' }],
  _collection: COLLECTION,
  list() { return this._items.slice(); },
  add(w) { if (!this._items.some((i) => i.token === w.token)) this._items.push({ token: w.token }); },
  remove(t) { this._items = this._items.filter((i) => i.token !== t); },
  collection() { return this._collection; },
  setCollection(t) { this._collection = t; },
};

global.Api = {
  async getCollection(token) {
    if (token !== COLLECTION) return null;
    return { token, watches: serverList.map((t) => watches[t]) };
  },
  async getWatch(token) { return watches[token] || null; },
  async removeFromCollection(token, watchToken) {
    calls.push(['removeFromCollection', token, watchToken]);
    serverList = serverList.filter((t) => t !== watchToken);
  },
  async addToCollection() {}, async createCollection() { return { token: COLLECTION }; },
  async createWatch() { throw new Error('未使用'); },
};

eval(fs.readFileSync(path.join(root, 'static', 'js', 'index.js'), 'utf8'));

const settle = () => new Promise((r) => setImmediate(r));

(async () => {
  // bootstrap（非同期）が終わるまで待つ。
  for (let i = 0; i < 40; i++) await settle();

  const list = byId('watchList');
  assert.strictEqual(list.children.length, 2, '最初に2件出ていること');

  // 1件目の「一覧から外す」を押す。li の子の最後がボタン。
  const row = list.children[0];
  const button = row.children[row.children.length - 1];
  assert.ok(button.listeners.click, 'ボタンに click が付いていること');
  await button.listeners.click[0]();
  for (let i = 0; i < 40; i++) await settle();

  // ★ 本題: サーバ側の一覧からも外したか
  assert.deepStrictEqual(
    calls, [['removeFromCollection', COLLECTION, 'w1']],
    'サーバ側の一覧からも外すこと（これを忘れると押しても消えない）');

  assert.deepStrictEqual(serverList, ['w2'], 'サーバ側から消えていること');
  assert.strictEqual(byId('watchList').children.length, 1, '再描画後に1件になること');
  assert.ok(!Store.list().some((i) => i.token === 'w1'),
            'localStorage に書き戻されていないこと');
  assert.notStrictEqual(byId('formError').textContent, 'mine.forget.failed',
                        '成功時に失敗の文言を出さないこと');

  console.log('remove_from_list: 通過');
})().catch((err) => { console.error(err); process.exit(1); });
