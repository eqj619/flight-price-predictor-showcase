/**
 * モニター画面。PI-27-flightTrend の4タブ構成
 * （ダッシュボード / 価格トレンド / 買い時分析 / 設定）を踏襲している。
 *
 * **実測とモデル推定を混ぜて見せない。** 実測が1件も無いときは
 * basis="simulated" が返るので、価格の隣に必ず「モデル推定」と明示し、
 * 3値判定は出さない。PI-27-flightTrend のダッシュボードは
 * simulated_prices しか読んでおらず、推定値が実測のように見えていた。
 */
(function () {
  const token = decodeURIComponent(window.location.pathname.replace(/^\/w\//, ''));
  const el = (id) => document.getElementById(id);

  let watch = null;
  let activeRouteIndex = 0;
  // 追いかけている便。ルートごとに別なので、ルートを変えたら捨てる。
  let activeFlightKey = null;          // 価格トレンドタブで見ているルート
  let selectedAirlines = new Set();  // モニターする航空会社（サーバに保存する）

  I18N.init();
  document.addEventListener('langchange', () => {
    renderMonitorSwitch();
    if (watch) renderAll();
  });
  wireTabs();
  load();

  async function load() {
    try {
      watch = await Api.getWatch(token);
    } catch {
      watch = null;
    }
    el('loading').hidden = true;

    if (!watch) {
      el('notFound').hidden = false;
      el('navTabs').hidden = true;
      Store.remove(token);      // サーバ側で消えているなら索引からも消す
      // 行き止まりにしない。他のモニターへ移れるようにしておく。
      renderMonitorSwitch();
      wireMonitorSwitch();
      return;
    }

    Store.add({
      token: watch.token,
      origin: watch.origin,
      destination: watch.destination,
      departure_date: watch.departure_date,
    });
    selectedAirlines = new Set(watch.airline_filter || []);
    renderMonitorSwitch();
    showTab('dashboard');
    renderAll();
    wireActions();
  }

  // ------------------------------------------------------------------
  // モニターの切り替え
  // ------------------------------------------------------------------

  /**
   * このブラウザで作ったモニターの一覧を出す。
   *
   * 索引は localStorage で、サーバに問い合わせない。ここで全件検証すると
   * 画面を開くたびに件数ぶんの API 呼び出しが増える。失効済みを選んだ場合は
   * 遷移先が「見つかりません」になり、そこで索引から落ちる（自己修復）。
   *
   * 切り替え先が無い（1件だけ）ときは出さない。
   */
  function renderMonitorSwitch() {
    const select = el('monitorSwitch');
    const items = Store.list()
      .slice()
      .sort((a, b) => String(a.departure_date).localeCompare(String(b.departure_date)));

    select.setAttribute('aria-label', I18N.t('nav.switchMonitor'));
    select.title = I18N.t('nav.switchMonitor');

    if (items.length < 2) {
      select.hidden = true;
      return;
    }

    select.replaceChildren(...items.map((w) => {
      const option = document.createElement('option');
      option.value = w.token;
      option.textContent = `${w.origin}→${w.destination}  ${w.departure_date}`;
      option.selected = w.token === token;
      return option;
    }));
    select.hidden = false;
  }

  function wireMonitorSwitch() {
    el('monitorSwitch').addEventListener('change', (event) => {
      const next = event.target.value;
      if (next && next !== token) window.location.href = `/w/${next}`;
    });
  }

  // ------------------------------------------------------------------
  // タブ
  // ------------------------------------------------------------------

  function wireTabs() {
    el('navTabs').addEventListener('click', (event) => {
      const tab = event.target.closest('.nav-tab');
      if (tab) showTab(tab.dataset.tab);
    });
  }

  function showTab(name) {
    document.querySelectorAll('.nav-tab').forEach((t) => {
      t.classList.toggle('active', t.dataset.tab === name);
    });
    document.querySelectorAll('.tab-content').forEach((section) => {
      const on = section.id === `tab-${name}`;
      section.classList.toggle('active', on);
      section.hidden = !on;
    });
    // canvas は表示された後でないとサイズが決まらない。
    if (name === 'trends') requestAnimationFrame(drawChart);
  }

  // ------------------------------------------------------------------

  function routes() {
    return watch.routes || [];
  }

  /**
   * 表示の根拠。確かな順に observed > transferred > simulated。
   *
   *   observed    この出発日そのものの実測がある
   *   transferred 別の出発日の実測から転用。**社名は出せる**
   *   simulated   このルートの実測が一切ない。社名は出さない
   */
  function basis() {
    const values = routes().map((r) => (r.summary || {}).basis);
    if (values.includes('observed')) return 'observed';
    // **便が無いことが分かったら、それを最優先で言う。** 他の区間の推定で
    // 上書きすると、存在しない便の値段を見せたまま黙ることになる。
    if (values.length && values.every((v) => v === 'no_flights')) return 'no_flights';
    // **未確認も推定より優先する。** 値段を出していないので、
    // 「モデルによる推定です」と言うと出していないものを説明してしまう。
    if (values.length && values.every((v) => v === 'unconfirmed')) return 'unconfirmed';
    if (values.includes('round_trip_observed')) return 'round_trip_observed';
    if (values.includes('transferred')) return 'transferred';
    if (values.includes('round_trip_pending')) return 'round_trip_pending';
    return 'simulated';
  }

  /** モデル推定を出しているか。
   *
   *  **往復は estimate ではない。** 往復では推定を一切出していないので
   *  （実測が無ければ空で返す）、「モデルによる推定です」と言うのは嘘になる。
   *  観測待ちであることは別の文言で伝える。
   */
  function isEstimate() {
    return basis() === 'transferred' || basis() === 'simulated';
  }

  function isRoundTripPending() { return basis() === 'round_trip_pending'; }
  function hasNoFlights() { return basis() === 'no_flights'; }
  function isUnconfirmed() { return basis() === 'unconfirmed'; }

  /** 社名を出してよいか。実測で観測された便だけが名乗れる。 */
  function hasAirlineNames() { return basis() !== 'simulated'; }

  function renderAll() {
    renderHeader();
    renderDashboard();
    renderTrendsTab();
    renderTiming();
    renderChangelog();
    renderSettings();
    renderMaxDuration();
    renderFxNote();
    // **グラフも描き直す。** 言語を切り替えると通貨が変わるので、
    // ここを呼ばないと軸とツールチップだけドル表示で取り残される。
    drawChart();
  }

  /** 更新履歴。中身は static/js/changelog.js（利用者から見た変更だけ）。 */
  function renderChangelog() {
    const entries = window.CHANGELOG || [];
    el('changelogBody').replaceChildren(...entries.map((release) => {
      const block = document.createElement('div');
      block.className = 'release';
      block.appendChild(div('release-date', release.date));

      const list = document.createElement('ul');
      list.className = 'release-items';
      release.items.forEach((item) => {
        const li = document.createElement('li');
        li.textContent = I18N.lang === 'en' ? item.en : item.ja;
        list.appendChild(li);
      });
      block.appendChild(list);
      return block;
    }));
  }

  function renderHeader() {
    const daysLeft = Math.max(daysBetween(new Date(), new Date(watch.departure_date)), 0);
    const ret = watch.return_date || '';
    el('subtitle').textContent =
      `${watch.origin} ${ret ? '⇄' : '→'} ${watch.destination} · `
      + `${I18N.t('watch.departure')} ${watch.departure_date} · `
      + (ret ? `${I18N.t('watch.return')} ${ret} · ` : '')
      + I18N.t('watch.daysLeft', { n: daysLeft });

    const enabled = (watch.next_check || {}).enabled;
    el('statusDot').className = `status-dot${enabled ? '' : ' off'}`;
    el('statusText').textContent = I18N.t(enabled ? 'status.online' : 'status.offline');
  }

  // ------------------------------------------------------------------
  // ダッシュボード
  // ------------------------------------------------------------------

  /** 実測が無いときの但し書き。**便が無い区間では「まだ」と言わない。** */
  function noDataLabel() {
    if (hasNoFlights()) return I18N.t('card.noFlights');
    if (isUnconfirmed()) return I18N.t('card.unconfirmed');
    return I18N.t('card.noRecords');
  }

  /**
   * 通知の誘い。**モニターを開いた最初の画面に出す。**
   *
   * 2026-08-29 に価格トレンドのタブへ置いたが、10日間で購読は3件のまま
   * 動かなかった。理由は場所で、**125件中84件（67%）は作った日以外に
   * 一度も開かれていない**（2026-09-08 実測）。戻ってこない人に見せる
   * 場所に置いていた。戻ってこないから通知が要るのに。
   *
   * 出さない条件は送信側と揃える。**送らない通知を誘うのは嘘になる。**
   */
  function renderNotifyOffer() {
    const box = el('notifyOffer');
    // **通知の設定は読み込みと並行して進む。** モニターが届く前に
    // 呼ばれることがあるので、watch を触る前に降りる。
    if (!box || !watch) return;
    box.textContent = '';
    box.hidden = true;

    if (typeof Push === 'undefined' || !pushState.enabled) return;
    if (pushState.subscribed || pushState.hintKey) return;
    if (Push.offerDismissed()) return;    // 断られた記憶は push.js が持つ

    // 往復には②を送らない（notify.py の return_date ガードと同じ理由）。
    if (watch.return_date) return;
    // **路線があるか確かめられていない間は誘わない。** 値段すら出して
    // いない区間で「ここから値上がりします」と知らせる約束はできない。
    if (hasNoFlights() || isUnconfirmed()) return;
    const route = routes().find((r) => !r.nearby);
    const rise = route && route.recommendation && route.recommendation.price_rises_after;
    if (!rise || rise.days <= 0) return;      // 開始日が過ぎていたら誘わない

    // 通知が鳴るのは開始日。そのとき出発まで何日かを出す。**送信側と
    // 同じ値を使う**（notify.py の days は開始日当日の残日数＝これ）。
    const atSend = rise.days_before_departure;

    box.hidden = false;
    const card = document.createElement('div');
    card.className = 'notify-offer';

    card.appendChild(span('notify-head',
      I18N.t('offer.head', { d: rise.date, n: rise.days })));

    // **届く文面をそのまま見せる。** 何が来るか分からないものは許可しにくい。
    const preview = document.createElement('div');
    preview.className = 'notify-preview';
    preview.appendChild(span('np-title', I18N.t('offer.sample.title', {
      o: route.origin, d: route.destination })));
    preview.appendChild(span('np-body', I18N.t('offer.sample.body', {
      departure: watch.departure_date, days: atSend,
      pct: rise.penalty_pct })));
    card.appendChild(preview);

    card.appendChild(span('notify-fine', I18N.t('offer.fine')));

    const row = document.createElement('div');
    row.className = 'notify-actions';
    const yes = document.createElement('button');
    yes.type = 'button';
    yes.className = 'small';
    yes.textContent = I18N.t('offer.yes');
    const no = document.createElement('button');
    no.type = 'button';
    no.className = 'ghost small';
    no.textContent = I18N.t('offer.no');
    const status = span('notify-fine', '');

    yes.addEventListener('click', async () => {
      yes.disabled = true;
      try {
        await Push.subscribe(token);
        pushState.subscribed = true;
        const toggle = el('pushToggle');
        if (toggle) toggle.checked = true;          // 設定側の表示も合わせる
        const line = el('pushStatus');
        if (line) line.textContent = I18N.t('push.pending');
        row.remove();
        status.textContent = I18N.t('offer.done');
        card.appendChild(status);
        drawChart();
      } catch (err) {
        yes.disabled = false;
        status.textContent = I18N.t(
          err.message === 'denied' ? 'push.denied' : 'push.failed');
        card.appendChild(status);
      }
    });
    no.addEventListener('click', () => {
      Push.dismissOffer();               // 一覧側の誘いも一緒に止まる
      box.hidden = true;
      box.textContent = '';
    });

    row.append(yes, no);
    card.appendChild(row);
    box.appendChild(card);
  }

  function renderDashboard() {
    renderNotifyOffer();
    renderModelNotice('modelNoticeDash');
    renderSummaryCards();
    renderRouteComparison();
    renderTopTable();
  }

  function renderModelNotice(containerId) {
    const box = el(containerId);
    box.textContent = '';

    // 便が見つからない区間。**ダッシュボードで最初に言う。**
    // 買い時分析まで開かないと分からないのでは遅い。
    if (hasNoFlights()) {
      const notice = document.createElement('div');
      notice.className = 'model-notice warn';
      const text = span('', '');
      text.innerHTML = I18N.t('route.noFlights');
      notice.append(span('', '🚫'), text);
      box.appendChild(notice);
      return;
    }

    // まだ一度も取得していない区間。**推定を出さないので、そう言う。**
    if (isUnconfirmed()) {
      const notice = document.createElement('div');
      notice.className = 'model-notice';
      const text = span('', '');
      text.innerHTML = I18N.t('route.unconfirmed');
      notice.append(span('', '⏳'), text);
      box.appendChild(notice);
      return;
    }

    // 往復で、まだ観測が無い場合。**推定は出していないので、そう言う。**
    if (isRoundTripPending()) {
      const notice = document.createElement('div');
      notice.className = 'model-notice';
      notice.append(span('', '⏳'), span('', I18N.t('rt.pending')));
      box.appendChild(notice);
      return;
    }
    if (!isEstimate()) return;

    const transferred = basis() === 'transferred';
    const rec = routes().map((r) => r.recommendation).find(Boolean) || {};

    const notice = document.createElement('div');
    notice.className = 'model-notice';
    notice.append(
      span('', transferred ? '📄' : '🧪'),
      span('', transferred
        ? I18N.t('transferred.notice', {
            n: rec.evidence_points || 0,
            d: rec.evidence_last_seen || '--',
          })
        : I18N.t('model.notice')),
    );
    box.appendChild(notice);
  }

  function estimateBadge() {
    return I18N.t(basis() === 'transferred' ? 'transferred.badge' : 'model.badge');
  }

  function renderSummaryCards() {
    const model = isEstimate();
    const summaries = routes().map((r) => r.summary || {});
    const best = summaries
      .map((s) => s.best)
      .filter(Boolean)
      .sort((a, b) => a.price_usd - b.price_usd)[0];

    const avgValues = summaries.map((s) => s.avg_price_usd).filter((v) => v != null);
    const avg = avgValues.length
      ? avgValues.reduce((a, b) => a + b, 0) / avgValues.length : null;
    const records = summaries.reduce((sum, s) => sum + (s.records || 0), 0);
    const days = Math.max(0, ...summaries.map((s) => s.days_monitored || 0));
    const next = watch.next_check || {};

    el('summaryCards').replaceChildren(
      card('icon-green', '💰',
        I18N.t(model ? 'card.bestEstimate' : 'card.bestPrice'),
        best ? money(best.price_usd) : '--',
        best ? bestDetail(best, model) : noDataLabel(), true),
      card('icon-indigo', '📊',
        I18N.t(model ? 'card.avgEstimate' : 'card.avgPrice'),
        avg ? money(avg) : '--',
        model ? I18N.t('card.overWindow') : I18N.t('card.records', { n: records })),
      card('icon-amber', '📅', I18N.t('card.monitorDays'), String(days),
        records ? I18N.t('card.records', { n: records }) : noDataLabel()),
      card('icon-cyan', '🔄', I18N.t('card.nextCheck'),
        next.enabled ? next.time : '--',
        next.enabled
          ? I18N.t('card.dailyAt', { t: next.time, tz: next.timezone })
          : I18N.t('card.manual')),
    );
  }

  function bestDetail(best, model) {
    // transferred なら社名が入っているので、そのまま出す。
    const who = best.airline
      ? `${best.airline} · ${stopsLabel(best)}` : estimateBadge();
    const when = model
      ? I18N.t('label.around', { d: best.check_date })
      : I18N.t('label.asOf', { d: best.check_date });
    return `${who} · ${when}`;
  }

  function card(iconClass, emoji, label, value, detail, green) {
    const wrap = document.createElement('div');
    wrap.className = 'summary-card';

    const icon = document.createElement('div');
    icon.className = `card-icon ${iconClass}`;
    icon.textContent = emoji;

    const body = document.createElement('div');
    body.className = 'card-body';
    body.append(
      span('card-label', label),
      span(`card-value${green ? ' green' : ''}`, value),
      span('card-detail', detail),
    );

    wrap.append(icon, body);
    return wrap;
  }

  function renderRouteComparison() {
    const model = isEstimate();
    el('routeBestSub').textContent = model ? estimateBadge() : '';

    el('routeComparison').replaceChildren(...routes().map((route) => {
      const best = (route.summary || {}).best;
      const wrap = document.createElement('div');
      wrap.className = 'route-card';

      const header = document.createElement('div');
      header.className = 'route-header';
      header.append(
        span('route-badge origin', route.origin),
        span('route-arrow', '→'),
        span('route-badge dest', route.destination),
      );
      if (route.nearby) {
        header.appendChild(span('badge badge-nearby', I18N.t('nearby.badge')));
      }

      const price = document.createElement('div');
      price.className = `route-price${best ? '' : ' muted'}`;
      price.textContent = best ? money(best.price_usd) : '--';

      const detail = document.createElement('div');
      detail.className = 'route-detail';
      detail.textContent = best
        ? (best.airline
            ? `${itineraryPath(route, best)} · ${best.airline}`
            : I18N.t('label.around', { d: best.check_date }))
        : noDataLabel();

      wrap.append(header, price, detail);
      return wrap;
    }));
  }

  /**
   * 実測があるときは「最安フライト TOP10」、無いときは「予測価格の推移」。
   *
   * モデル推定のときに航空会社別の行を出さないのは、実測が無い状態で
   * 社名を書くと、その社がその路線を飛んでいなくても飛んでいるように
   * 見えてしまうため（ZIPAIR は NRT-SAN の通し運賃を売っていない）。
   */
  function renderTopTable() {
    // 社名を出せるなら（実測 or 別日程からの転用）便の一覧、
    // 出せないなら日付ごとの価格推移。
    const named = hasAirlineNames();
    el('topListTitle').textContent = I18N.t(named ? 'dash.top10' : 'dash.forecast');

    if (!named) {
      el('topListSub').textContent = I18N.t('dash.forecastSub');
      el('topTable').replaceChildren(...forecastTable());
      return;
    }

    const rec = routes().map((r) => r.recommendation).find(Boolean) || {};
    el('topListSub').textContent = isEstimate()
      ? I18N.t('transferred.airlineNote', { n: rec.evidence_points || 0 })
      : I18N.t('dash.asOf', { d: watch.latest_check_date || '--' });
    el('topTable').replaceChildren(...cheapestTable());
  }

  function cheapestTable() {
    const estimate = isEstimate();
    const rows = [];
    for (const route of routes()) {
      for (const series of route.series) {
        // 実測があればその最新値、無ければ推定の初日（＝いまの想定価格）。
        const point = estimate
          ? series.projected[0]
          : series.observed[series.observed.length - 1];
        if (!point) continue;
        rows.push({
          price: point.price_usd, date: point.check_date,
          route: routePath(route, series),
          nearby: route.nearby, ground: needsGroundTransfer(route),
          airline: series.airline, series,
          totalMinutes: series.total_minutes,
          lower: point.lower_usd, upper: point.upper_usd,
        });
      }
    }
    rows.sort((a, b) => a.price - b.price);

    // 経由地は ROUTE 表記に含めたので、独立した「経由」列は置かない。
    const keys = ['th.rank', 'th.route', 'th.airline', 'th.duration', 'th.price'];
    if (estimate) keys.push('th.band');
    keys.push('th.status');

    const body = document.createElement('tbody');
    rows.slice(0, 10).forEach((row, i) => {
      const tr = document.createElement('tr');
      tr.append(
        cell(rankBadge(i + 1)),
        cell(row.route + (row.ground ? ' 🚗' : '')),
        cell(row.airline),
        cell(duration(row.totalMinutes)),
        cell(money(row.price), 'price-cell'),
      );
      // 推定のときは幅を必ず併記する。点推定だけ出すと実測と区別がつかない。
      if (estimate) tr.append(cell(`${money(row.lower)} – ${money(row.upper)}`));
      tr.append(cell(phaseLabel(row.series)));
      body.appendChild(tr);
    });
    return [headerRow(keys), body];
  }

  /**
   * 総所要（飛行 + 乗継）の表示。
   *
   * この列を持つ前に集めた実測は None なので、0 や「—」ではなく
   * 「不明」と分かる形にする。0h00 と出すと直行便のように見えてしまう。
   */
  function duration(minutes) {
    if (minutes == null) return '–';
    return `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, '0')}`;
  }

  /**
   * 実際に通る空港を並べた表記。1経由なら NRT-SFO-SAN のように経由地を挟む。
   *
   * 便ごとに経由地が違うので、モニターの (出発, 到着) だけでは
   * 「どこを通るのか」が分からない。表の行は便そのものなので、行を見れば
   * 経路が読めるようにする。
   */
  function routePath(route, series) {
    const stops = series.via_airport ? [series.via_airport] : [];
    return [route.origin, ...stops, route.destination].join('-');
  }

  function forecastTable() {
    // 週次に間引く。67日分を全部並べても読めない。
    const route = routes()[0];
    const points = (route.series[0] || { projected: [] }).projected;
    const head = headerRow(['th.date', 'th.price', 'th.band', 'th.status']);
    const body = document.createElement('tbody');

    points.filter((_p, i) => i % 7 === 0).slice(0, 12).forEach((point) => {
      const tr = document.createElement('tr');
      tr.append(
        cell(point.check_date),
        cell(money(point.price_usd), 'price-cell'),
        cell(`${money(point.lower_usd)} – ${money(point.upper_usd)}`),
        cell(windowLabel(point.check_date)),
      );
      body.appendChild(tr);
    });
    return [head, body];
  }

  /**
   * その日が値上がり局面かどうか。**「底値帯」という言い方はしない。**
   * 2026-08-15 の実測で、待って安くなる時期は無いと分かった（曲線は
   * 出発52日前より前が平坦）。言えるのは「ここから先は上がる」だけ。
   */
  function windowLabel(checkDate) {
    const r = routes()[0].recommendation;
    const rise = r && r.price_rises_after;
    if (!rise) return '';
    return checkDate >= rise.date
      ? I18N.t('status.rising') : I18N.t('status.flat');
  }

  function phaseLabel(series) {
    const first = series.projected[0];
    if (!first) return I18N.t('status.monitoring');
    const r = routes()[0].recommendation;
    const rise = r && r.price_rises_after;
    if (rise && first.check_date >= rise.date) return I18N.t('status.rising');
    return I18N.t('status.monitoring');
  }

  // ------------------------------------------------------------------
  // 価格トレンド
  // ------------------------------------------------------------------

  function renderTrendsTab() {
    renderModelNotice('modelNoticeTrends');
    el('chartSwitch').replaceChildren(...routes().map((route, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = index === activeRouteIndex ? 'active' : '';
      button.textContent = `${route.origin} → ${route.destination}`;
      if (needsGroundTransfer(route)) button.textContent += ' 🚗';
      if (route.active === false) button.textContent += ' ⏸';
      button.addEventListener('click', () => {
        activeRouteIndex = index;
        activeFlightKey = null;      // 便はルートごとに別
        renderTrendsTab();
        drawChart();
      });
      return button;
    }));
    if (!el('tab-trends').hidden) drawChart();
  }

  function drawChart() {
    const route = routes()[activeRouteIndex];
    if (!route) return;
    const rise = route.recommendation && route.recommendation.price_rises_after;
    renderFlightSwitch(route);

    // **描くものが無いときは、白紙ではなく理由を出す。**
    // 往復は実測が無ければ推定で埋めないので、ここが空になりうる。
    if (!(route.series || []).length) {
      const box = el('priceChart');
      box.replaceChildren();
      const note = document.createElement('p');
      note.className = 'hint chart-empty';
      note.textContent = I18N.t(watch.return_date ? 'chart.emptyRoundTrip'
                                                  : 'chart.empty');
      box.appendChild(note);
      renderTimingNote(route, rise);
      return;
    }

    PriceChart.render(el('priceChart'), route.series, rise, activeFlightKey,
                      jpyRate());
    renderTimingNote(route, rise);
  }

  /** 便を一意に指す文字列。**chart.js の keyOf と同じ形にすること。** */
  function flightKey(s) {
    return `${s.stops}|${s.via_airport || ''}|${s.airline}`
      + `|${s.departure_time || ''}`;
  }

  /** 出発時刻の 'HH:MM'。取れていない便は空。 */
  function departsAt(s) {
    const t = s.departure_time || '';
    return t.includes('T') ? t.split('T')[1] : '';
  }

  /** その便の直近の価格。実測が無ければ推定の初日。 */
  function latestPrice(s) {
    const obs = s.observed || [];
    if (obs.length) return obs[obs.length - 1].price_usd;
    const proj = s.projected || [];
    return proj.length ? proj[0].price_usd : Infinity;
  }

  /**
   * 追いかける便を選ぶ。**既定は今いちばん安い便。**
   *
   * 「どれを見ればいいか分からない」状態にしないため、最初から
   * 買う可能性が最も高い便を出しておく。
   */
  function renderFlightSwitch(route) {
    const select = el('flightSelect');
    if (!select) return;
    const list = [...route.series].sort((a, b) => latestPrice(a) - latestPrice(b));
    if (!list.length) { select.replaceChildren(); return; }
    if (!list.some((s) => flightKey(s) === activeFlightKey)) {
      activeFlightKey = flightKey(list[0]);
    }
    select.replaceChildren(...list.map((s) => {
      const opt = document.createElement('option');
      opt.value = flightKey(s);
      opt.selected = opt.value === activeFlightKey;
      const days = (s.observed || []).length;
      // **出発時刻を出す。** これが無いと、同じ社・同じ経路の別便が
      // 見分けられない一覧になる（1日に何本も飛ぶ）。
      const at = departsAt(s);
      opt.textContent = `${money(latestPrice(s))} · ${itineraryPath(route, s)}`
        + (at ? ` · ${at}` : '')
        + ` · ${s.airline}`
        + (days ? ` · ${I18N.t('chart.observedDays', { n: days })}` : '');
      return opt;
    }));
  }

  /**
   * 買い時の要約。**期限までの残り日数で言うことが変わる。**
   *
   * 2026-08-15 までここは「あと◯日で底値帯に入ります」と言っていた。
   * 実測（212系列 / 13,004点）で、待って安くなる時期は無いと分かったので
   * 反転させた。曲線は出発52日前より前が平坦で、45〜75日前とそれ以前に
   * 差が無い（32/66系列, 符号検定 z=-0.2）。一方、出発30日前を過ぎると
   * +16.7%（35/41系列, z=+4.5）。
   *
   * **待つ理由を与える文言を書かないこと。** 待っても得るものが無く、
   * 取り逃す危険だけが増える。伝えるべきは「いつまでに買うか」。
   */
  /**
   * 通知の状態。**買い時の案内から購読を誘うために持つ。**
   *
   * 設定タブ（4番目）の中だけに置いていたら、126件のモニターに対して
   * 購読が5件しか無かった（2026-08-29 の KPI）。利用者が最初に見るのは
   * Dashboard で、設定まで辿り着かない。値上がり開始日を読んでいる、
   * いちばん動機の高いところで誘う。
   */
  let pushState = { enabled: false, subscribed: false, hintKey: null };

  /**
   * 「その日が来たら知らせる」の導線。
   *
   * **出さない条件を守ること。** 送らない通知を誘うのは嘘になる。
   * - 往復は②を送らない（notify.py の return_date ガードと同じ理由）
   * - 開始日が過ぎていたら送らない（「これから上がります」の知らせなので）
   */
  function riseNotifyPrompt() {
    if (typeof Push === 'undefined' || !pushState.enabled) return null;

    // 受け取れない端末には、手順を1行だけ置く。**ここが読まれる場所**で、
    // 設定タブまで辿り着く人はごく一部。
    if (pushState.hintKey) return span('sweet-basis', I18N.t(pushState.hintKey));
    if (pushState.subscribed) return null;

    const wrap = document.createElement('span');
    wrap.className = 'sweet-cta';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'ghost small';
    button.textContent = I18N.t('rise.notify.cta');
    const status = span('sweet-basis', '');
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await Push.subscribe(token);
        pushState.subscribed = true;
        status.textContent = I18N.t('rise.notify.done');
        button.remove();
        // 設定タブのチェックも合わせる。二重に持たせない。
        const box = el('pushToggle');
        if (box) { box.checked = true; el('pushStatus').textContent = I18N.t('push.pending'); }
      } catch (err) {
        button.disabled = false;
        status.textContent = I18N.t(
          err.message === 'denied' ? 'push.denied' : 'push.failed');
      }
    });
    wrap.append(button, status);
    return wrap;
  }

  function renderTimingNote(route, rise) {
    const box = el('sweetSpotNote');
    if (!box) return;
    box.textContent = '';
    if (!rise) {
      // 出発済みなど。黙って消すのではなく、枠ごと隠す。
      box.hidden = true;
      return;
    }
    box.hidden = false;

    let text;
    if (rise.days > 0) {
      box.className = 'sweet-note';
      text = I18N.t('rise.ahead', { n: rise.days, d: rise.date });
    } else {
      // すでに上昇局面。**ここで初めて緊急性を言う。**
      box.className = 'sweet-note past';
      text = I18N.t('rise.now', { pct: rise.penalty_pct });
    }
    box.appendChild(span('sweet-main', text));
    box.appendChild(span('sweet-basis', I18N.t('rise.basis')));

    // **これから上がる片道にだけ誘う。** 往復には②を送らないし、
    // 既に上がっている場合は知らせる先の日が無い。
    if (rise.days > 0 && !watch.return_date) {
      const prompt = riseNotifyPrompt();
      if (prompt) box.appendChild(prompt);
    }
  }

  // ------------------------------------------------------------------
  // 買い時分析
  // ------------------------------------------------------------------

  function renderTiming() {
    const body = el('timingBody');
    body.textContent = '';

    const savings = nearbySavings();
    for (const route of routes()) {
      const section = document.createElement('section');
      section.className = 'card';

      const heading = document.createElement('h2');
      heading.textContent = `${route.origin} → ${route.destination}`;
      if (route.nearby) {
        heading.appendChild(span('badge badge-nearby', I18N.t('nearby.badge')));
      }
      section.appendChild(heading);

      if (route.active === false) {
        // 黙って空にしない。データが来ない理由を必ず言う。
        // **「取れない」と「便が無い」を言い分ける。** 前者はこちらの
        // 不調、後者はその区間が運航されていない可能性。利用者が次に
        // 取るべき行動が違う（待つ / 別の空港を試す）。
        const html = route.stopped_kind === 'no_flights'
          ? I18N.t('route.noFlights') : I18N.t('route.stopped');
        const note = para('stopped', '');
        note.innerHTML = html;
        section.appendChild(note);
      }
      if (route.nearby) {
        section.appendChild(para('hint', nearbyNote(route)));
        if (savings && savings.destination === route.destination) {
          section.appendChild(para('savings', I18N.t('nearby.cheaper', {
            a: route.destination, n: savings.amount.toLocaleString(),
          })));
        }
      }

      const rec = route.recommendation;
      if (!rec) {
        // しぼり込みで消えたのか、そもそも何も無いのかを言い分ける。
        section.appendChild(para('hint',
          selectedAirlines.size ? I18N.t('filter.noMatch') : noDataLabel()));
      } else {
        section.appendChild(verdictBox(rec));
        section.appendChild(factList(rec, route));
      }
      body.appendChild(section);
    }
  }

  /**
   * 到着地が希望と違う便か。**🚗 はこの条件だけに付ける。**
   *
   * 出発地だけが違う便（HND→SAN の監視に対する NRT→SAN）は、
   * 到着地が希望どおりなので陸路の移動は発生しない。出発地が違うことは
   * ボタンの表記（NRT → SAN）自体が示しており、アイコンで重ねて言う必要がない。
   */
  function needsGroundTransfer(route) {
    return route.destination !== watch.destination;
  }

  /** 近接空港ルールで足されたルートの説明。どちら側が違うかで言い分ける。 */
  function nearbyNote(route) {
    const originDiffers = route.origin !== watch.origin;
    const destDiffers = needsGroundTransfer(route);
    if (originDiffers && destDiffers) {
      return I18N.t('nearby.noteBoth', { o: route.origin, d: route.destination });
    }
    if (originDiffers) {
      return I18N.t('nearby.noteDeparture', { a: route.origin });
    }
    return I18N.t('nearby.noteArrival', { a: route.destination });
  }

  function verdictBox(rec) {
    // **往復には買い時判定を出さない。**
    // 判定は価格曲線から出るが、曲線は片道 212系列から推定したもので、
    // 往復では遠い側の平坦さしか確かめられていない。谷（50〜59日前）と
    // 遅延の罰（30日前以降）は未測定 — 判定の中身はその2つで決まる。
    // 実測（いくらだったか）は観測そのものなので出す。
    if (rec.basis === 'round_trip_observed') {
      const box = document.createElement('div');
      box.className = 'verdict verdict-round-trip';
      box.append(
        div('verdict-headline', I18N.t('rt.headline')),
        div('verdict-why', I18N.t('rt.why')),
        div('verdict-evidence', I18N.t('label.observedDays', { n: rec.observed_days })),
      );
      return box;
    }
    const box = document.createElement('div');
    box.className = `verdict verdict-${rec.verdict}`;
    box.append(
      div('verdict-headline', I18N.t(`verdict.${rec.verdict}`)),
      div('verdict-why', I18N.t(`verdict.${rec.verdict}.why`)),
      // 判定と「実測◯日分」は必ずセットで出す。実測1日分の判定を
      // 40日分と同じ重みで受け取られないようにするため。
      div('verdict-evidence', I18N.t('label.observedDays', { n: rec.observed_days })),
    );
    if (rec.recommended_check_date) {
      box.appendChild(div('verdict-date',
        `${I18N.t('label.recommended')}: ${rec.recommended_check_date}`));
    }
    return box;
  }

  function factList(rec, route) {
    const list = document.createElement('dl');
    list.className = 'facts';

    if (rec.current_best) {
      addFact(list, I18N.t('label.currentBest'),
        `${money(rec.current_best.price_usd)} · ${itineraryPath(route, rec.current_best)}`
        + ` · ${rec.current_best.airline}`,
        I18N.t('label.asOf', { d: rec.current_best.check_date }));
    }
    // 過去最安は「記録」であって買える価格ではない。今の最安と同じ日なら重ねない。
    if (rec.lowest_observed && rec.current_best
        && rec.lowest_observed.check_date !== rec.current_best.check_date) {
      addFact(list, I18N.t('label.lowestSeen'),
        `${money(rec.lowest_observed.price_usd)} · ${rec.lowest_observed.airline}`,
        I18N.t('label.record', { d: rec.lowest_observed.check_date }));
    }
    if (rec.predicted_min) {
      const p = rec.predicted_min;
      // **確度の低い数字を、確かな数字と同じ見た目で並べない。**
      // 出発30日以内では信頼帯に入るのが約6割（2026-08-24 の検証）。
      addFact(list, I18N.t('label.predictedMin'),
        `${money(p.price_usd)} (${money(p.lower_usd)} – ${money(p.upper_usd)})`
        + ` · ${itineraryPath(route, p)}`
        + (p.airline ? ` · ${p.airline}` : ` · ${I18N.t('label.modelAirline')}`),
        [I18N.t('label.around', { d: p.check_date }),
         rec.basis === 'transferred'
           ? I18N.t('transferred.airlineNote', { n: rec.evidence_points || 0 })
           : null].filter(Boolean).join(' · '));
      // 金額のすぐ下に、どれだけ当てにしてよいかを書く。
      const note = document.createElement('dd');
      note.className = 'fact-note';
      note.textContent = I18N.t('label.predictedNote');
      list.appendChild(document.createElement('dt'));
      list.appendChild(note);
    }
    const rise = rec.price_rises_after;
    if (rise) {
      addFact(list, I18N.t('rise.title'),
        rise.days > 0
          ? I18N.t('rise.value', { d: rise.date, n: rise.days })
          : I18N.t('rise.valuePast', { d: rise.date }),
        I18N.t('rise.note', {
          a: rise.days_before_departure, pct: rise.penalty_pct,
        }));
    }
    return list;
  }

  function addFact(list, term, value, note) {
    const dt = document.createElement('dt');
    dt.textContent = term;
    const dd = document.createElement('dd');
    dd.textContent = value;
    if (note) {
      const small = document.createElement('div');
      small.className = 'meta';
      small.textContent = note;
      dd.appendChild(small);
    }
    list.append(dt, dd);
  }

  /** 近接空港のほうが安いなら、その差額。実測でも推定でも同じ基準で比べる。 */
  function nearbySavings() {
    const priceOf = (r) => ((r || {}).summary || {}).best;
    const primary = priceOf(routes().find((r) => !r.nearby));
    if (!primary) return null;

    let best = null;
    for (const alt of routes().filter((r) => r.nearby)) {
      const candidate = priceOf(alt);
      if (candidate && candidate.price_usd < primary.price_usd
          && (!best || candidate.price_usd < best.price)) {
        best = { label: `${alt.origin} → ${alt.destination}`,
                 price: candidate.price_usd };
      }
    }
    return best
      ? { destination: best.label, amount: Math.round(primary.price_usd - best.price) }
      : null;
  }

  // ------------------------------------------------------------------
  // 設定
  // ------------------------------------------------------------------

  function renderSettings() {
    renderNearbyToggle();
    renderAirlineChoices();
    el('shareUrl').value = `${window.location.origin}/w/${watch.token}`;

    const next = watch.next_check || {};
    const records = routes().reduce((sum, r) => sum + ((r.summary || {}).records || 0), 0);
    const info = el('sysInfo');
    info.textContent = '';
    addFact(info, I18N.t('info.records'), String(records));
    addFact(info, I18N.t('info.lastCollection'), watch.latest_check_date || I18N.t('info.none'));
    addFact(info, I18N.t('info.nextCollection'),
      next.enabled ? (next.at || '').replace('T', ' ').slice(0, 16) : I18N.t('card.manual'));
    addFact(info, I18N.t('info.schedule'),
      next.enabled ? `${next.time} (${next.timezone})` : I18N.t('info.none'));
    addFact(info, I18N.t('info.created'), (watch.created_at || '').slice(0, 10));
    addFact(info, I18N.t('info.expires'), watch.expires_at);
  }

  /**
   * モニターする航空会社の複数選択。
   *
   * サーバに保存する（表示だけの絞り込みではない）。共有 URL を開いた全員に
   * 同じ条件が見えるべきだし、買い時判定の入力もこれで変わるため。
   * 何も選ばない = 全社が対象。
   */
  function renderAirlineChoices() {
    const available = watch.available_airlines || [];
    const seen = new Set(
      routes().flatMap((r) => r.series.map((s) => s.airline)).filter(Boolean),
    );

    el('airlineEmpty').hidden = available.length > 0;
    el('airlineChoices').replaceChildren(...available.map((name) => {
      const label = document.createElement('label');
      label.className = 'airline-choice';

      const box = document.createElement('input');
      box.type = 'checkbox';
      box.value = name;
      box.checked = selectedAirlines.has(name);
      box.addEventListener('change', () => {
        if (box.checked) selectedAirlines.add(name);
        else selectedAirlines.delete(name);
        label.classList.toggle('checked', box.checked);
        updateAirlineStatus();
      });

      label.classList.toggle('checked', box.checked);
      label.append(box, span('', name));
      // いま選ばれているが、この期間に観測されていない社は明示する。
      if (!seen.has(name)) {
        label.appendChild(span('meta', I18N.t('filter.notSeen')));
      }
      return label;
    }));

    updateAirlineStatus();
  }

  function updateAirlineStatus() {
    el('airlineStatus').textContent = selectedAirlines.size
      ? I18N.t('filter.selected', { n: selectedAirlines.size })
      : I18N.t('filter.allSelected');
  }

  function wireAirlineActions() {
    el('airlineAll').addEventListener('click', () => {
      (watch.available_airlines || []).forEach((a) => selectedAirlines.add(a));
      renderAirlineChoices();
    });
    el('airlineNone').addEventListener('click', () => {
      selectedAirlines.clear();
      renderAirlineChoices();
    });
    el('airlineSave').addEventListener('click', async () => {
      const button = el('airlineSave');
      button.disabled = true;
      button.textContent = I18N.t('filter.saving');
      try {
        await Api.updateWatch(token, { airline_filter: [...selectedAirlines] });
        // 判定と較正の入力が変わるので、素直に取り直す。
        watch = await Api.getWatch(token);
        selectedAirlines = new Set(watch.airline_filter || []);
        renderAll();
        el('airlineStatus').textContent = I18N.t('filter.saved');
      } catch (err) {
        el('airlineStatus').textContent = err.message;
      } finally {
        button.disabled = false;
        button.textContent = I18N.t('filter.save');
      }
    });
  }

  /**
   * 所要時間の上限。
   *
   * 航空会社の絞り込みと同じく、表示だけでなく較正と買い時判定の入力からも
   * 外れる。**除外件数を必ず出す** — 黙って消すと「安い便が理由なく消えた」
   * ようにしか見えない。
   */
  function renderMaxDuration() {
    const select = el('maxDuration');
    const current = watch.max_total_minutes || 0;
    // 既定値が選択肢に無い場合に備えて、無ければ足す。
    if (![...select.options].some((o) => Number(o.value) === current)) {
      const opt = document.createElement('option');
      opt.value = String(current);
      opt.textContent = current ? duration(current) : I18N.t('settings.noLimit');
      select.appendChild(opt);
    }
    select.value = String(current);

    const n = routes().reduce((sum, r) => sum + (r.excluded_by_duration || 0), 0);
    el('maxDurationExcluded').textContent = current
      ? I18N.t(n ? 'settings.durationExcluded' : 'settings.durationNone', { n })
      : '';
  }

  function wireMaxDuration() {
    el('maxDuration').addEventListener('change', async (event) => {
      const select = event.target;
      const previous = String(watch.max_total_minutes || 0);
      select.disabled = true;
      el('maxDurationStatus').textContent = I18N.t('settings.saving');
      try {
        await Api.updateWatch(token, { max_total_minutes: Number(select.value) });
        watch = await Api.getWatch(token);
        renderAll();
        el('maxDurationStatus').textContent = I18N.t('filter.saved');
      } catch (err) {
        el('maxDurationStatus').textContent = err.message;
        select.value = previous;
      } finally {
        select.disabled = false;
      }
    });
  }

  /**
   * 近接空港ルールの on/off。
   *
   * **収集対象そのものが変わる**ので、航空会社の絞り込み（表示と判定だけ）
   * とは別のカードに置いてある。切ると誰も見ていないルートの収集が止まる。
   */
  function renderNearbyToggle() {
    const box = el('nearbyToggle');
    box.checked = watch.nearby_airports !== false;
    el('nearbyRow').classList.toggle('checked', box.checked);
    el('nearbyRoutes').textContent = I18N.t('settings.nearbyRoutes', {
      routes: routes().map((r) => `${r.origin}→${r.destination}`).join(' / '),
    });
  }

  /* 通知の購読（課題4）。
   *
   * **鍵が未設定ならカードごと出さない。** 「通知を受け取る」と見せて
   * 何も来ないのが一番まずい。対応していないブラウザでも同じ。
   */
  async function wirePushToggle() {
    if (typeof Push === 'undefined') return;
    const card = el('pushCard');
    const box = el('pushToggle');
    const status = el('pushStatus');
    if (!card || !box) return;

    // **押される前に公開鍵を取っておく。** 押された瞬間に fetch すると、
    // Safari では許可ダイアログが出なくなる（利用者の操作と結び付いた
    // 処理の中でしか requestPermission を呼べない）。
    let cfg = { enabled: false };
    try { cfg = await Push.prime(); } catch { /* 出さないだけ */ }
    if (!cfg.enabled) return;          // サーバ側で無効。カードは隠したまま
    pushState.enabled = true;

    card.hidden = false;
    if (!Push.supported) {
      // **押しても何も起きない操作を残さない。**
      // 無効にしたチェックボックスを置くと、利用者は押してから初めて
      // 「反応しない」と気づく。説明が下にあっても、押した指の下からは
      // 見えない。操作ごと消して、手順だけを残す。
      const row = el('pushRow');
      if (row) row.hidden = true;
      // **iPhone・iPad を「非対応」で片付けない。** タブでは受け取れない
      // だけで、ホーム画面に追加すれば使える。手順まで書く。
      // **どのブラウザで開いているかで案内が変わる。**
      // iOS の Chrome や Firefox は中身が WebKit なので Safari と同じ
      // 制約を受けるが、ホーム画面への追加は Safari から行うのが確実。
      pushState.hintKey = Push.isIOSNonSafari ? 'push.iosOtherBrowser'
        : Push.needsHomeScreen ? 'push.ios'
          : 'push.unsupported';
      status.textContent = I18N.t(pushState.hintKey);
      status.className = 'hint';
      drawChart();                     // 案内側にも手順を出す
      renderNotifyOffer();
      return;
    }

    try {
      box.checked = await Push.isSubscribed(token);
      pushState.subscribed = box.checked;
      status.textContent = I18N.t(box.checked ? 'push.on' : 'push.off');
    } catch { status.textContent = ''; }
    // **ここまで来て初めて誘える。** 公開鍵が揃う前にボタンを出すと、
    // 押された瞬間に fetch が挟まり Safari で許可ダイアログが出ない。
    drawChart();
    renderNotifyOffer();

    box.addEventListener('change', async (event) => {
      const on = event.target.checked;
      box.disabled = true;
      status.textContent = I18N.t('settings.saving');
      try {
        if (on) {
          await Push.subscribe(token);
          pushState.subscribed = true;
          drawChart();                 // 案内側の誘いを引っ込める
          renderNotifyOffer();
          // **いつから効くかを言う。** 判定は日次ジョブなので、押した直後に
          // 何か届くわけではない。黙っていると壊れていると思われる。
          status.textContent = I18N.t('push.pending');
        } else {
          await Push.unsubscribe(token);
          pushState.subscribed = false;
          drawChart();                 // 誘いを戻す
          status.textContent = I18N.t('push.off');
        }
      } catch (err) {
        box.checked = !on;
        status.textContent = I18N.t(
          err.message === 'denied' ? 'push.denied'
            : err.message === 'unsupported' ? 'push.unsupported'
              : 'push.failed');
      } finally {
        box.disabled = false;
      }
    });
  }

  function wireNearbyToggle() {
    el('nearbyToggle').addEventListener('change', async (event) => {
      const box = event.target;
      box.disabled = true;
      el('nearbyStatus').textContent = I18N.t('settings.saving');
      try {
        await Api.updateWatch(token, { nearby_airports: box.checked });
        watch = await Api.getWatch(token);
        renderAll();
        el('nearbyStatus').textContent = I18N.t('filter.saved');
      } catch (err) {
        el('nearbyStatus').textContent = err.message;
        box.checked = !box.checked;
      } finally {
        box.disabled = false;
      }
    });
  }

  function wireActions() {
    wireMonitorSwitch();
    const flightSelect = el('flightSelect');
    if (flightSelect) {
      flightSelect.addEventListener('change', (event) => {
        activeFlightKey = event.target.value;
        drawChart();
      });
    }
    wireNearbyToggle();
    // 通知は非同期に用意する。失敗しても画面は出す。
    wirePushToggle().catch(() => {});
    wireMaxDuration();
    wireAirlineActions();
    el('copyBtn').addEventListener('click', async (event) => {
      try {
        await navigator.clipboard.writeText(el('shareUrl').value);
      } catch {
        el('shareUrl').select();
      }
      event.target.textContent = I18N.t('watch.copied');
      setTimeout(() => { event.target.textContent = I18N.t('watch.copy'); }, 1500);
    });

    el('deleteBtn').addEventListener('click', async () => {
      if (!window.confirm(I18N.t('watch.deleteConfirm'))) return;
      await Api.deleteWatch(token);
      Store.remove(token);
      window.location.href = '/';
    });
  }

  // ------------------------------------------------------------------
  // 小物
  // ------------------------------------------------------------------

  function headerRow(keys) {
    const thead = document.createElement('thead');
    const tr = document.createElement('tr');
    keys.forEach((key) => {
      const th = document.createElement('th');
      th.textContent = I18N.t(key);
      tr.appendChild(th);
    });
    thead.appendChild(tr);
    return thead;
  }

  function cell(content, className) {
    const td = document.createElement('td');
    if (className) td.className = className;
    if (content instanceof Node) td.appendChild(content);
    else td.textContent = content;
    return td;
  }

  function rankBadge(n) {
    return span(`rank${n <= 3 ? ` rank-${n}` : ''}`, String(n));
  }

  function span(className, text) {
    const node = document.createElement('span');
    node.className = className;
    node.textContent = text;
    return node;
  }

  function div(className, text) {
    const node = document.createElement('div');
    node.className = className;
    node.textContent = text;
    return node;
  }

  function para(className, text) {
    const node = document.createElement('p');
    node.className = className;
    node.textContent = text;
    return node;
  }

  /**
   * 価格の表示。日本語のときは当日レートで円に換算する。
   *
   * **全期間を1つのレートで換算する。** 観測日ごとのレートを使うと、運賃が
   * 1円も動いていないのにグラフが動く（実測: 為替は3か月で 4.6% の幅があり、
   * 同じ $700 が ¥109,676〜¥114,737 になる）。1つのレートなら単なる定数倍
   * なので、線の形＝値動きはそのまま保たれる。
   *
   * 100円単位に丸めるのは、元の USD が既に整数に丸めた値だから。
   * ¥111,531 と出すと持っていない精度を主張することになる。
   */
  function money(value) {
    const rate = jpyRate();
    // **`$` だけだと USD なのか CAD/AUD/SGD/HKD なのか分からない。**
    // 通貨記号を単独で使わず、必ず国を伴わせる。表の桁揃えを崩さないよう
    // 接尾（1,043 USD）ではなく接頭にする。
    if (!rate) return `US$${Math.round(value).toLocaleString()}`;
    return `¥${(Math.round(value * rate / 100) * 100).toLocaleString()}`;
  }

  /**
   * 何の通貨で出しているかを必ず示す。**カードは常に出す。**
   *
   * 以前は円に換算したときだけ出していたが、それだと英語表示の利用者は
   * どこにも通貨の説明が無いまま `$` を見ることになる。`$` は USD 以外
   * （CAD / AUD / SGD / HKD など）でも使われるので、これは実際に
   * 「どこの $ か分からない」という指摘につながった。
   */
  function renderFxNote() {
    const card = el('fxCard');
    if (!card) return;
    card.hidden = false;
    const rate = jpyRate();
    el('fxNote').textContent = rate
      ? I18N.t('fx.note', { rate: rate.toFixed(2), date: watch.fx.date })
      : I18N.t('fx.usdOnly');
  }

  /** 換算に使うレート。日本語表示で、かつ取得できているときだけ。 */
  function jpyRate() {
    if (I18N.lang !== 'ja') return null;
    const fx = watch && watch.fx;
    return fx && fx.rate ? fx.rate : null;
  }

  /**
   * 便が実際に通る空港を並べた表記。1経由なら SAN-LAX-HND。
   *
   * **「LAX 経由」ではなく経路そのものを出す。** 買い時分析では価格が
   * 通し運賃（SAN→LAX→HND 全体の運賃）であることが読み取れず、区間運賃に
   * 見えるという指摘を受けた。経路を出せばその誤解が起きない。
   */
  function itineraryPath(route, item) {
    if (!route || !item) return '';
    const via = item.stops && item.via_airport ? [item.via_airport] : [];
    return [route.origin, ...via, route.destination].join('-');
  }

  function stopsLabel(item) {
    if (item.stops === 0) return I18N.t('label.nonstop');
    return item.via_airport
      ? I18N.t('label.via', { a: item.via_airport })
      : I18N.t('label.oneStop');
  }

  function daysBetween(from, to) {
    return Math.round((to - from) / 86400000);
  }
})();
