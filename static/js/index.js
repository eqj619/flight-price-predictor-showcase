/** トップページ: モニターの登録と、このブラウザから作った一覧。 */
(function () {
  const form = document.getElementById('createForm');
  const errorBox = document.getElementById('formError');
  const submitBtn = document.getElementById('submitBtn');
  const listEl = document.getElementById('watchList');
  const emptyNote = document.getElementById('emptyNote');

  I18N.init();
  // 空港マスタは重い（97KB）ので、入力欄に触れるまで取りに行かない。
  ['origin', 'destination'].forEach((name) => {
    const input = form.querySelector(`input[name="${name}"]`);
    if (input) Airports.attach(input);
  });
  document.addEventListener('langchange', renderList);
  primeDateInput();
  bootstrap();

  /**
   * 一覧トークンを確定させてから描画する。
   *
   * 優先順位は URL > localStorage。**URL が勝つ**のは、別端末で
   * /l/{token} を開いたときにその一覧が出てほしいため。localStorage を
   * 優先すると、ブックマークから開いても手元の一覧が表示されてしまう。
   */
  async function bootstrap() {
    const fromUrl = location.pathname.startsWith('/l/')
      ? decodeURIComponent(location.pathname.slice(3)) : null;
    if (fromUrl) {
      const found = await Api.getCollection(fromUrl).catch(() => null);
      if (found) Store.setCollection(fromUrl);
    }
    await migrateLocalWatches();
    renderList();
  }

  /**
   * 一覧を持っていない利用者のための移行。
   *
   * この仕組みより前に作られたモニターは localStorage にしかない。
   * **黙って一覧に入れる** — 利用者に操作を求めると、求められたことに
   * 気づかないまま localStorage を消して全部失う人が残る。
   */
  async function migrateLocalWatches() {
    if (Store.collection()) return;
    const local = Store.list();
    if (!local.length) return;
    try {
      const created = await Api.createCollection();
      Store.setCollection(created.token);
      for (const item of local) {
        await Api.addToCollection(created.token, item.token);
      }
    } catch {
      /* 作れなくても従来どおり localStorage で動く */
    }
  }

  /** 一覧を用意する（無ければ作る）。モニター作成時に呼ぶ。 */
  async function ensureCollection() {
    const existing = Store.collection();
    if (existing) return existing;
    const created = await Api.createCollection();
    Store.setCollection(created.token);
    return created.token;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    errorBox.hidden = true;
    submitBtn.disabled = true;
    submitBtn.textContent = I18N.t('form.creating');

    const data = new FormData(form);
    const airlines = (data.get('airlines') || '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);

    try {
      // 片道のときは return_date を送らない（null ではなく未指定にする）。
      const roundTrip = data.get('trip_type') === 'round_trip';
      const payload = {
        origin: data.get('origin'),
        destination: data.get('destination'),
        departure_date: data.get('departure_date'),
        airline_filter: airlines,
      };
      if (roundTrip) payload.return_date = data.get('return_date');
      const created = await Api.createWatch(payload);
      Store.add({
        token: created.token,
        origin: created.origin,
        destination: created.destination,
        departure_date: created.departure_date,
        return_date: created.return_date || '',
      });
      // 一覧にも入れる。失敗しても登録自体は成立しているので止めない。
      try {
        await Api.addToCollection(await ensureCollection(), created.token);
      } catch { /* 一覧に入らなくてもモニターは使える */ }
      // 作ったらそのままモニター画面へ。
      window.location.href = created.url;
    } catch (err) {
      errorBox.textContent = err.message;
      errorBox.hidden = false;
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = I18N.t('form.submit');
    }
  });

  const copyBtn = document.getElementById('copyCollection');
  if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
      const url = document.getElementById('collectionUrl').value;
      try {
        await navigator.clipboard.writeText(url);
      } catch {
        // クリップボードが使えない環境（http や権限拒否）では選択にとどめる。
        document.getElementById('collectionUrl').select();
        return;
      }
      copyBtn.textContent = I18N.t('collection.copied');
      setTimeout(() => { copyBtn.textContent = I18N.t('collection.copy'); }, 1600);
    });
  }

  function primeDateInput() {
    // 出発が近すぎる日付を初期値にしない。60日は「底値帯の真ん中」の
    // つもりだったが、2026-08-15 の実測でその帯は無いと分かった。
    // **値としては据え置く** — これは監視を始める既定日であって、
    // いつ買えという助言ではない。60日先なら値上がり前の平坦部に入る。
    const tripType = form.querySelector('#tripType');
    const returnField = form.querySelector('#returnField');
    const returnInput = form.querySelector('input[name="return_date"]');
    const departureInput = form.querySelector('input[name="departure_date"]');

    const syncTripType = () => {
      const rt = tripType.value === 'round_trip';
      returnField.hidden = !rt;
      returnInput.required = rt;
      if (!rt) returnInput.value = '';
    };
    tripType.addEventListener('change', syncTripType);
    // 復路は出発日より後しか選べないようにする（サーバ側でも弾く）。
    const syncReturnMin = () => {
      if (!departureInput.value) return;
      const d = new Date(departureInput.value);
      d.setDate(d.getDate() + 1);
      returnInput.min = d.toISOString().slice(0, 10);
      if (returnInput.value && returnInput.value < returnInput.min) {
        returnInput.value = returnInput.min;
      }
    };
    departureInput.addEventListener('change', syncReturnMin);
    syncTripType();
    syncReturnMin();

    const input = form.querySelector('input[name="departure_date"]');
    const today = new Date();
    input.min = toIso(today);
    const suggested = new Date(today);
    suggested.setDate(suggested.getDate() + 60);
    input.value = toIso(suggested);
  }

  function toIso(d) {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }

  async function renderList() {
    listEl.textContent = '';
    const token = Store.collection();

    // **一覧を持っていればサーバ側が正。** localStorage は使わない。
    // 端末をまたいでも、ブックマークから開けば同じ内容になる。
    let items = Store.list();
    if (token) {
      const collection = await Api.getCollection(token).catch(() => null);
      if (collection) {
        items = collection.watches;
        // 手元の索引も追随させる（一覧トークンを失った場合の保険）。
        items.forEach((w) => Store.add(w));
      } else {
        Store.setCollection(null);     // 失効していた
      }
    }
    renderCollectionBanner(Store.collection());

    // **サーバから返った中身のほうを持ち回る。** items は一覧トークンが
    // 無いとき localStorage 由来で、トークンしか入っていない。誘いの帯は
    // 路線と判定を見るので、それでは常に0件になる。
    const liveWatches = [];
    for (const item of items) {
      const alive = await Api.getWatch(item.token).catch(() => null);
      if (!alive) {
        // 消えたモニターは**サーバ側の一覧からも外す。** localStorage
        // だけ消しても一覧に残り続け、毎回 404 を引きに行くことになる。
        Store.remove(item.token);
        if (token) await Api.removeFromCollection(token, item.token).catch(() => {});
        continue;
      }
      liveWatches.push(alive);
      listEl.appendChild(renderItem(alive));
    }
    emptyNote.hidden = listEl.children.length > 0;
    renderNotifyBanner(liveWatches);
  }

  /**
   * 値上がりの日を知らせられることを、一覧の上で伝える（課題4）。
   *
   * **本命はモニター画面の誘い**（作った直後に必ず通る唯一の場所）で、
   * ここはその補助。125件中84件は作った日以外に開かれないので、戻って
   * きた人にしか届かないが、戻ってきた人には確実に見せられる。
   *
   * 出す条件は送信側と揃える。**送らない通知を誘えば嘘になる。**
   * 往復・路線が未確定・値上がり開始日を過ぎたものは数に入れない。
   */
  async function renderNotifyBanner(items) {
    const box = document.getElementById('notifyBanner');
    if (!box) return;
    box.textContent = '';
    box.hidden = true;
    if (typeof Push === 'undefined' || !Push.supported) return;
    if (Push.offerDismissed()) return;

    let cfg = { enabled: false };
    try { cfg = await Push.prime(); } catch { return; }
    if (!cfg.enabled) return;          // 鍵が無い。**誘って何も来ないのが最悪**

    // 購読済みは1回の問い合わせでまとめて除く（モニターごとに聞かない）。
    let already = [];
    try { already = await Push.subscribedWatches(); } catch { /* 全部未購読とみなす */ }

    const eligible = items.filter((w) => canBeNotified(w)
                                      && !already.includes(w.token));
    if (!eligible.length) return;

    box.hidden = false;
    const card = document.createElement('div');
    card.className = 'notify-offer';

    const head = document.createElement('span');
    head.className = 'notify-head';
    head.textContent = I18N.t('banner.head', { n: eligible.length });
    card.appendChild(head);

    const fine = document.createElement('span');
    fine.className = 'notify-fine';
    fine.textContent = I18N.t('banner.fine');
    card.appendChild(fine);

    const row = document.createElement('div');
    row.className = 'notify-actions';
    // **許可はここで求めない。** 通知はモニター単位なので、どれについて
    // 知らせるのかが決まっていない場所で許可だけ取ると、押した人が
    // 「何が来るのか」を知らないまま許可することになる。文面を見せられる
    // モニター画面へ送る。
    const go = document.createElement('a');
    go.className = 'link-button small';
    go.href = `/w/${eligible[0].token}`;
    go.textContent = I18N.t('banner.go');
    const no = document.createElement('button');
    no.type = 'button';
    no.className = 'ghost small';
    no.textContent = I18N.t('offer.no');
    no.addEventListener('click', () => {
      Push.dismissOffer();             // モニター画面の誘いも一緒に止まる
      box.hidden = true;
      box.textContent = '';
    });
    row.append(go, no);
    card.appendChild(row);
    box.appendChild(card);
  }

  /** そのモニターに②（値上がり開始）を送れるか。notify.py と同じ条件。 */
  function canBeNotified(watch) {
    if (watch.return_date) return false;
    const primary = (watch.routes || []).find((r) => !r.nearby);
    if (!primary) return false;
    // 路線があると確かめられていない間は誘わない。値段すら出していない。
    const basis = (primary.summary || {}).basis;
    if (basis === 'no_flights' || basis === 'unconfirmed') return false;
    const rise = primary.recommendation
      && primary.recommendation.price_rises_after;
    return !!rise && rise.days > 0;
  }

  /** 一覧の URL を見せる。**これを控えれば全部戻る**ことを伝える。 */
  function renderCollectionBanner(token) {
    const box = document.getElementById('collectionBanner');
    if (!box) return;
    box.hidden = !token;
    if (!token) return;
    const url = `${location.origin}/l/${token}`;
    document.getElementById('collectionUrl').value = url;
  }

  function renderItem(watch) {
    const li = document.createElement('li');

    const main = document.createElement('a');
    main.href = `/w/${watch.token}`;
    main.className = 'watch-link';
    const rt = watch.return_date || '';
    main.textContent = `${watch.origin} ${rt ? '⇄' : '→'} ${watch.destination}`;

    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = `${I18N.t('watch.departure')} ${watch.departure_date}`
      + (rt ? ` · ${I18N.t('watch.return')} ${rt}` : '');

    const verdict = firstVerdict(watch);
    if (verdict) {
      const badge = document.createElement('span');
      badge.className = `badge badge-${verdict}`;
      badge.textContent = I18N.t(`verdict.${verdict}`);
      main.appendChild(badge);
    }

    const forget = document.createElement('button');
    forget.type = 'button';
    forget.className = 'ghost small';
    forget.textContent = I18N.t('mine.forget');
    forget.addEventListener('click', async () => {
      // 一覧から消すだけ。サーバ上のモニターは消さない
      // （URL を知っている他の人がまだ見ているかもしれない）。
      //
      // **サーバ側の一覧からも外すこと。** 一覧トークンを持っていると
      // renderList はサーバ側を正として localStorage を上書きし、しかも
      // Store.add で書き戻す。localStorage だけ消しても、次の描画で
      // そのまま戻ってきて「押しても消えない」ように見える。
      forget.disabled = true;
      try {
        const collection = Store.collection();
        if (collection) await Api.removeFromCollection(collection, watch.token);
        Store.remove(watch.token);
        await renderList();
      } catch (err) {
        // 消せなかったことを黙らせない。黙ると元の症状に戻る。
        forget.disabled = false;
        errorBox.textContent = I18N.t('mine.forget.failed');
        errorBox.hidden = false;
      }
    });

    li.append(main, meta, forget);
    return li;
  }

  function firstVerdict(watch) {
    const primary = (watch.routes || []).find((r) => !r.nearby);
    return primary && primary.recommendation ? primary.recommendation.verdict : null;
  }
})();
