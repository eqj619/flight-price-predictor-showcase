/**
 * 自分のモニター一覧（localStorage）と API クライアント。
 *
 * **localStorage は正ではない。** サーバのトークンが正で、こちらは
 * 「このブラウザから作ったものの索引」でしかない。localStorage を消しても
 * URL さえあれば復旧できる必要があるし、逆にサーバ側で失効したものは
 * 一覧から消えるべき。
 */
const STORE_KEY = 'fppWatches';
const COLLECTION_KEY = 'fppCollection';

const Store = {
  list() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORE_KEY) || '[]');
      return Array.isArray(raw) ? raw : [];
    } catch {
      return [];
    }
  },

  add(watch) {
    const items = this.list().filter((w) => w.token !== watch.token);
    items.unshift(watch);
    localStorage.setItem(STORE_KEY, JSON.stringify(items));
  },

  remove(token) {
    localStorage.setItem(
      STORE_KEY,
      JSON.stringify(this.list().filter((w) => w.token !== token)),
    );
  },

  /**
   * 一覧トークン。**これ1つが失われなければ、モニターは全部戻る。**
   *
   * localStorage に置いているのは利便のためで、正はサーバ側の一覧。
   * 消えても /l/{token} をブックマークしていれば復旧できる。
   */
  collection() {
    return localStorage.getItem(COLLECTION_KEY) || null;
  },

  setCollection(token) {
    if (token) localStorage.setItem(COLLECTION_KEY, token);
    else localStorage.removeItem(COLLECTION_KEY);
  },
};

const Api = {
  async createWatch(payload) {
    const response = await fetch('/api/watches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw await toError(response);
    return response.json();
  },

  async getWatch(token) {
    const response = await fetch(`/api/watches/${encodeURIComponent(token)}`);
    if (response.status === 404) return null;
    if (!response.ok) throw await toError(response);
    return response.json();
  },

  async updateWatch(token, payload) {
    const response = await fetch(`/api/watches/${encodeURIComponent(token)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw await toError(response);
  },

  async createCollection() {
    const response = await fetch('/api/collections', { method: 'POST' });
    if (!response.ok) throw await toError(response);
    return response.json();
  },

  async getCollection(token) {
    const response = await fetch(`/api/collections/${encodeURIComponent(token)}`);
    if (response.status === 404) return null;
    if (!response.ok) throw await toError(response);
    return response.json();
  },

  async addToCollection(token, watchToken) {
    const response = await fetch(
      `/api/collections/${encodeURIComponent(token)}/watches/${encodeURIComponent(watchToken)}`,
      { method: 'PUT' });
    if (!response.ok && response.status !== 404) throw await toError(response);
    return response.ok;
  },

  async removeFromCollection(token, watchToken) {
    const response = await fetch(
      `/api/collections/${encodeURIComponent(token)}/watches/${encodeURIComponent(watchToken)}`,
      { method: 'DELETE' });
    // **失敗を握り潰さない。** 消せていないのに消えたことにすると、
    // 次の描画でサーバ側から戻ってきて「押しても消えない」に見える。
    // 404 は一覧ごと消えている場合で、目的は達しているので通す。
    if (!response.ok && response.status !== 404) throw await toError(response);
  },

  async deleteWatch(token) {
    const response = await fetch(`/api/watches/${encodeURIComponent(token)}`, {
      method: 'DELETE',
    });
    if (!response.ok && response.status !== 404) throw await toError(response);
  },
};

async function toError(response) {
  let detail = I18N.t('error.generic');
  try {
    const body = await response.json();
    if (typeof body.detail === 'string') {
      detail = body.detail;
    } else if (Array.isArray(body.detail) && body.detail.length) {
      // pydantic の検証エラー
      detail = body.detail.map((d) => d.msg).join(' / ');
    }
  } catch {
    /* レスポンスが JSON でないときは既定文言のまま */
  }
  return new Error(detail);
}

window.Store = Store;
window.Api = Api;
