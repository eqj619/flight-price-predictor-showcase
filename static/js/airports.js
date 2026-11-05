/**
 * 空港コードの入力補完。
 *
 * **補完は入力を楽にするためのもので、保証ではない。** 利用者は候補を無視して
 * 直接入力できるし、API を直接叩くこともできる。拒否はサーバ側（app/airports.py）
 * が行う。両者が同じ static/airports.json を見ているので、画面で選べるのに
 * サーバが拒否する、という食い違いは起きない。
 *
 * マスタは 296KB（gzip 後 97KB）。**入力欄に最初に触れた時点で初めて取りに行く。**
 * 読み込み後の照合はすべてこの中で行うので、打鍵ごとのリクエストは発生しない。
 * $7 のインスタンスに毎打鍵のリクエストを乗せるより、1回送って以降を即時に
 * するほうが速く、サーバも軽い。
 */
(function () {
  const SIZE_ORDER = { l: 0, m: 1, s: 2 };
  const MAX_RESULTS = 8;

  let master = null;         // { airports: [...], metros: {...} }
  let loading = null;        // 多重取得を防ぐ

  function load() {
    if (master) return Promise.resolve(master);
    if (loading) return loading;
    loading = fetch('/static/airports.json')
      .then((r) => {
        if (!r.ok) throw new Error('airports.json ' + r.status);
        return r.json();
      })
      .then((raw) => {
        master = {
          airports: raw.airports.map((a) => ({
            code: a[0], name: a[1], city: a[2] || '', country: a[3],
            size: a[4], aliases: a[5] || [], runway: a[6] || 0,
          })),
          metros: raw.metros || {},
        };
        return master;
      })
      .catch((err) => {
        // 補完が使えなくてもフォームは使える（コードを直接打てる）。
        // 黙って壊れるより、候補を出さないほうが害が小さい。
        console.warn('空港マスタを読み込めませんでした', err);
        loading = null;
        throw err;
      });
    return loading;
  }

  /**
   * 一致の強さ。小さいほど上に出す。-1 は不一致。
   *
   * コード完全一致を最優先にするのは、コードを知っている利用者が「SAN」と
   * 打ったとき、San Francisco や San Juan に埋もれさせないため。
   */
  function score(a, q, upper) {
    if (a.code === upper) return 0;
    if (a.code.startsWith(upper)) return 1;
    const city = a.city.toLowerCase();
    const name = a.name.toLowerCase();
    if (city.startsWith(q)) return 2;
    if (name.startsWith(q) || a.aliases.some((x) => x.toLowerCase().startsWith(q))) return 3;
    // **短い入力では部分一致を採らない。** 3文字以下はコードを打っている
    // ことがほとんどで、拾うと無関係な候補で埋まる（"TYO" で Artyom、
    // Shakhtyorsk が出た）。
    if (q.length < 4) return -1;
    if (city.includes(q)) return 4;
    if (name.includes(q) || a.aliases.some((x) => x.toLowerCase().includes(q))) return 5;
    return -1;
  }

  function search(query) {
    if (!master) return [];
    const q = query.trim().toLowerCase();
    if (!q) return [];
    const upper = q.toUpperCase();

    const scored = [];
    for (const a of master.airports) {
      const s = score(a, q, upper);
      if (s >= 0) scored.push({ airport: a, s });
    }
    // 一致の強さ → 空港の規模 → 滑走路長。最後まで並びを決めきる。
    // コード順で決着させると、"san" で SFO が28件の後ろに沈む。
    scored.sort((x, y) =>
      x.s - y.s
      || SIZE_ORDER[x.airport.size] - SIZE_ORDER[y.airport.size]
      || y.airport.runway - x.airport.runway
      || x.airport.code.localeCompare(y.airport.code));

    let rows = scored.slice(0, MAX_RESULTS).map((x) => ({ type: 'airport', airport: x.airport }));

    // 都市コード（TYO, OSA…）は空港ではないので、そのままでは選べない。
    // **所属空港を候補として差し込む。** 見出しだけ出して「自分で探せ」に
    // すると、TYO と打った人が結局 HND/NRT を知らないと進めない。
    const members = master.metros[upper];
    if (members && members.length) {
      const already = new Set(rows.map((r) => r.airport.code));
      const injected = members.map(byCode).filter((a) => a && !already.has(a.code))
        .map((a) => ({ type: 'airport', airport: a }));
      rows = [{ type: 'metro', code: upper, members }, ...injected, ...rows]
        .slice(0, MAX_RESULTS + 1);
    }
    return rows;
  }

  function byCode(code) {
    if (!master) return null;
    return master.airports.find((a) => a.code === code) || null;
  }

  /** 入力欄1つにコンボボックスを付ける。 */
  function attach(input) {
    const wrap = document.createElement('div');
    wrap.className = 'combo';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    const list = document.createElement('ul');
    list.className = 'combo-list';
    list.hidden = true;
    list.setAttribute('role', 'listbox');
    wrap.appendChild(list);

    const chosen = document.createElement('p');
    chosen.className = 'combo-chosen';
    wrap.appendChild(chosen);

    // maxlength を外す。付いたままだと "tokyo" や "成田" が打てない。
    input.removeAttribute('maxlength');
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');

    let rows = [];
    let active = -1;

    function close() {
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      active = -1;
    }

    function describe() {
      const a = byCode(input.value.trim().toUpperCase());
      chosen.textContent = a ? `${a.name}${a.city ? ' · ' + a.city : ''}, ${a.country}` : '';
    }

    function render() {
      list.replaceChildren(...rows.map((row, i) => {
        const li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.className = i === active ? 'active' : '';
        if (row.type === 'metro') {
          li.className += ' combo-metro';
          li.textContent = I18N.t('combo.allAirports', {
            code: row.code, list: row.members.join(' / '),
          });
        } else {
          const a = row.airport;
          const code = document.createElement('span');
          code.className = 'combo-code';
          code.textContent = a.code;
          const text = document.createElement('span');
          text.className = 'combo-text';
          text.textContent = `${a.name}${a.city ? ' · ' + a.city : ''}, ${a.country}`;
          li.append(code, text);
          li.addEventListener('mousedown', (e) => {   // blur より先に拾う
            e.preventDefault();
            pick(i);
          });
        }
        return li;
      }));
      list.hidden = rows.length === 0;
      input.setAttribute('aria-expanded', String(rows.length > 0));
    }

    function pick(i) {
      const row = rows[i];
      if (!row || row.type === 'metro') return;
      input.value = row.airport.code;
      close();
      describe();
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function refresh() {
      rows = search(input.value);
      active = rows.findIndex((r) => r.type === 'airport');
      render();
      describe();
    }

    input.addEventListener('focus', () => {
      load().then(refresh).catch(() => { /* 補完なしで続行 */ });
    });
    input.addEventListener('input', () => {
      if (master) refresh();
      else load().then(refresh).catch(() => {});
    });
    input.addEventListener('blur', () => setTimeout(close, 120));
    input.addEventListener('keydown', (event) => {
      if (list.hidden) return;
      const movable = rows.map((r, i) => (r.type === 'airport' ? i : -1)).filter((i) => i >= 0);
      if (!movable.length) return;
      const at = movable.indexOf(active);
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        active = movable[(at + 1) % movable.length];
        render();
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        active = movable[(at - 1 + movable.length) % movable.length];
        render();
      } else if (event.key === 'Enter' && active >= 0) {
        event.preventDefault();      // 候補を選ぶ間は送信しない
        pick(active);
      } else if (event.key === 'Escape') {
        close();
      }
    });
  }

  window.Airports = { attach, load, search, byCode };
})();
