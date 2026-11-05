/**
 * 言語切り替え。PI-27-flightTrend の public/js/i18n.js の作りを流用している。
 * 辞書 + data-i18n 属性 + localStorage で、サーバの言語設定に依存しない。
 */
const SUPPORTED = ['ja', 'en'];

/**
 * 初回に表示する言語を決める。
 *
 * **利用者が選んだ言語（localStorage）が最優先。** 検出はそれが無いときだけ。
 * 一度でも切り替えた人の選択を、ブラウザ設定で上書きしてはいけない。
 *
 * 検出は navigator.languages を先頭から見て、最初に対応する言語を採る。
 * 順序に意味があるので、ja が含まれるかだけを見てはいけない
 * （['en-US','ja'] の人は英語を望んでいる）。
 *
 * **どれとも一致しなければ英語。** 以前は日本語に倒していたため、米国の
 * 利用者が開いても日本語で表示されるという苦情が出た。ドメインは .com で
 * 利用者は世界中にいるので、中立な既定は英語のほうが妥当。
 */
function detectLang() {
  const stored = localStorage.getItem('fppLang');
  if (SUPPORTED.includes(stored)) return stored;

  const prefs = (navigator.languages && navigator.languages.length)
    ? navigator.languages
    : [navigator.language || ''];
  for (const raw of prefs) {
    const base = String(raw).toLowerCase().split('-')[0];
    if (SUPPORTED.includes(base)) return base;
  }
  return 'en';
}

const I18N = {
  _lang: detectLang(),

  get lang() { return this._lang; },

  dict: {
    'app.title':        { ja: '航空券 価格予測', en: 'Flight Price Predictor' },
    'app.tagline':      { ja: '出発日までの価格推移を予測し、買い時をお知らせします',
                          en: 'We track prices to your departure date and tell you when to buy.' },
    'lang.toggle':      { ja: '🌐 EN', en: '🌐 日本語' },
    'status.online':    { ja: '収集稼働中', en: 'Collecting daily' },
    'status.offline':   { ja: '自動収集オフ', en: 'Auto-collect off' },

    // ── タブ ──
    'nav.dashboard':    { ja: '📊 ダッシュボード', en: '📊 Dashboard' },
    'nav.trends':       { ja: '📈 価格トレンド', en: '📈 Price Trends' },
    'nav.timing':       { ja: '🎯 買い時分析', en: '🎯 Buy Timing' },
    'nav.changelog':    { ja: '📝 更新履歴', en: '📝 What\u2019s new' },
    'nav.settings':     { ja: '⚙️ 設定', en: '⚙️ Settings' },
    'nav.newMonitor':   { ja: '＋ 新しいモニター', en: '+ New monitor' },
    'nav.switchMonitor':{ ja: 'モニターを切り替える', en: 'Switch monitor' },

    // ── 登録フォーム ──
    'form.title':       { ja: 'モニターを作る', en: 'Create a monitor' },
    'combo.allAirports': {
      ja: '{code} は都市コードです。空港を選んでください: {list}',
      en: '{code} is a city code — pick an airport: {list}' },
    'form.origin':      { ja: '出発空港', en: 'From' },
    'form.destination': { ja: '到着空港', en: 'To' },
    'form.departure':   { ja: '出発日', en: 'Departure date' },
    'form.airlines':    { ja: '航空会社をしぼる（任意・カンマ区切り）',
                          en: 'Filter airlines (optional, comma separated)' },
    'form.submit':      { ja: 'モニターを作る', en: 'Create monitor' },
    'form.creating':    { ja: '作成中…', en: 'Creating…' },
    'form.hint':        { ja: 'IATA の3文字コードで入力してください（例: NRT, LAX）',
                          en: 'Use 3-letter IATA codes (e.g. NRT, LAX).' },

    // ── 自分のモニター ──
    'mine.title':       { ja: '自分のモニター', en: 'Your monitors' },
    'mine.empty':       { ja: 'まだありません。上のフォームから作ってください。',
                          en: 'None yet. Create one above.' },
    'mine.note':        { ja: 'この一覧はこのブラウザにだけ保存されています。URL を控えておけば、別の端末からも開けます。',
                          en: 'This list is stored only in this browser. Keep the URL to open it elsewhere.' },
    'collection.lead': {
      ja: 'この URL をブックマークしてください。ブラウザのデータを消しても、ここから一覧ごと元に戻せます。',
      en: 'Bookmark this URL. Even if you clear your browser data, it restores your whole list.' },
    'collection.copy':  { ja: 'URL をコピー', en: 'Copy URL' },
    'collection.copied': { ja: 'コピーしました', en: 'Copied' },
    'collection.note': {
      ja: 'モニターを追加すると自動でこの一覧に入ります。URL を知っている人は誰でも一覧を開けます。',
      en: 'New monitors are added to this list automatically. Anyone with the URL can open it.' },
    'mine.forget':      { ja: '一覧から消す', en: 'Remove from list' },

    // ── モニター画面 ──
    'watch.loading':    { ja: '読み込み中…', en: 'Loading…' },
    'watch.notFound':   { ja: 'このモニターは見つかりませんでした。削除されたか、有効期限が切れています。',
                          en: 'This monitor was not found. It was deleted or has expired.' },
    'watch.departure':  { ja: '出発日', en: 'Departure' },
    'watch.return':     { ja: '復路', en: 'Return' },
    // 通知（課題4）。**送る条件をそのまま書く。** 何が来るか分からない
    // まま許可させない。
    'mine.forget.failed': { ja: '一覧から外せませんでした。通信を確かめて、もう一度試してください。',
                            en: 'Could not remove it from the list. Check your connection and try again.' },
    'rise.notify.cta':  { ja: 'その日が来たら知らせる',
                          en: 'Notify me on that day' },
    'rise.notify.done': { ja: 'その日にお知らせします。1回だけお送りします。',
                          en: 'We will let you know on that day \u2014 once, and only once.' },
    'banner.head': { ja: '{n}件のモニターで、値上がりが始まる日をお知らせできます。',
                     en: 'We can tell you the day fares start climbing on {n} of your monitors.' },
    'banner.fine': { ja: '受け取るモニターを開いて、届く文面を確かめてから決められます。',
                     en: 'Open a monitor to see the exact notice before you decide.' },
    'banner.go':   { ja: '内容を見る', en: 'See what we would send' },
    'offer.head':   { ja: '{d}（あと{n}日）から、この便の値上がりが始まります。その日にお知らせしますか。',
                      en: 'Fares on this flight start climbing on {d}, in {n} days. Shall we tell you that day?' },
    'offer.sample.title': { ja: '{o}→{d} はここから値上がりします',
                            en: 'Fares start climbing: {o} to {d}' },
    'offer.sample.body':  { ja: '出発 {departure} まで {days} 日。ここから先は出発日に近づくほど上がり、出発30日前を過ぎると平均{pct}%高くなります。',
                            en: '{days} days until departure on {departure}. From here fares rise as departure approaches, averaging {pct}% higher once you are inside 30 days.' },
    'offer.fine':   { ja: '送るのはこの1回だけです。メールアドレスは受け取りません。ブラウザの設定でいつでも解除できます。',
                      en: 'This is the only notice we send for this monitor. No email address, and you can revoke it any time in your browser settings.' },
    'offer.yes':    { ja: 'その日に知らせる', en: 'Tell me that day' },
    'offer.no':     { ja: 'いいえ',           en: 'No thanks' },
    'offer.done':   { ja: 'お知らせします。次の収集（毎日03:00 太平洋時間）から判定します。',
                      en: 'We will let you know. Evaluation starts with the next collection (03:00 Pacific, daily).' },
    'push.title':       { ja: '通知', en: 'Notifications' },
    'push.note':        { ja: '次の2つのときだけお知らせします。(1) 監視を始めてから最も安い値を1%以上更新したとき (2) 値上がりが始まる日に届いたとき（1件につき1回だけ）。それ以外では送りません。往復では(2)を送りません（往復での値上がりをまだ実測できていないため）。',
                          en: 'We notify you in two cases only: (1) the price sets a new low, at least 1% below anything seen since you started watching, and (2) the day fares start climbing (once per monitor). Nothing else. Round trips do not get (2) \u2014 we have not yet measured how round-trip fares climb.' },
    'push.label':       { ja: 'このモニターの通知を受け取る', en: 'Notify me about this monitor' },
    'push.privacy':     { ja: 'メールアドレスは受け取りません。通知を届けるためにこの端末を指す文字列（ブラウザが発行するもの）だけを保存します。ブラウザの設定でいつでも解除できます。',
                          en: 'We do not collect your email address. To deliver notifications we store only the identifier your browser issues for this device. You can revoke it at any time in your browser settings.' },
    'push.on':          { ja: '通知を受け取ります。', en: 'Notifications are on.' },
    'push.off':         { ja: '通知は受け取りません。', en: 'Notifications are off.' },
    'push.denied':      { ja: 'ブラウザで通知が拒否されています。ブラウザの設定から許可し直してください。',
                          en: 'Notifications are blocked in your browser. Re-enable them in your browser settings.' },
    'push.unsupported': { ja: 'このブラウザは通知に対応していません。',
                          en: 'This browser does not support notifications.' },
    'push.iosOtherBrowser': { ja: 'iPhone・iPad では、ホーム画面に追加したアプリとして開いたときだけ通知を受け取れます。ブラウザのタブでは受け取れません（Chrome や Firefox も中身は Safari と同じ仕組みのため、同じ制約があります）。お手数ですが Safari でこのページを開き、共有ボタン（□に↑）から「ホーム画面に追加」を選び、追加されたアイコンから開き直してください。',
                          en: 'On iPhone and iPad, notifications arrive only when the site runs as a Home Screen app \u2014 not in a browser tab. Chrome and Firefox on iOS use the same engine as Safari, so they share this limit. Please open this page in Safari, tap the Share button (the square with an arrow), choose \u201cAdd to Home Screen\u201d, then reopen from the new icon.' },
    'push.ios':         { ja: 'iPhone・iPad では、ホーム画面に追加したときだけ通知を受け取れます。画面下の共有ボタン（□に↑）から「ホーム画面に追加」を選び、追加されたアイコンから開き直してください。このページのまま通知を受け取ることはできません。',
                          en: 'On iPhone and iPad, notifications work only after you add this site to your Home Screen. Tap the Share button (the square with an arrow), choose \u201cAdd to Home Screen\u201d, then reopen from the new icon. Notifications cannot be delivered to this tab.' },
    'push.failed':      { ja: '通知の設定に失敗しました。時間をおいて試してください。',
                          en: 'Could not set up notifications. Please try again later.' },
    'push.pending':     { ja: '通知を受け取ります。次の収集（毎日03:00 太平洋時間）から判定します。',
                          en: 'Notifications are on. We start evaluating from the next collection run (03:00 Pacific, daily).' },
    'rt.pending':       { ja: 'このモニターはまだ価格を1件も取得していません。往復では、実測が無いときにモデルによる推定を表示しません（片道で作った推定を往復に当てはめる根拠がないため）。次回の収集で記録が始まります。',
                          en: 'This monitor has no price observations yet. For round trips we do not fill the gap with model estimates \u2014 our model was fitted on one-way fares and we have no basis for applying it here. Recording starts at the next collection run.' },
    'chart.empty':      { ja: 'まだ描けるデータがありません。',
                          en: 'No data to plot yet.' },
    'chart.emptyRoundTrip': { ja: 'まだ実測がないため、グラフはありません。往復では推定で埋めない方針のため、次回の収集を待ってから表示されます。',
                          en: 'No chart yet \u2014 there are no observations. For round trips we do not plot estimates, so the chart appears after the next collection run.' },
    'form.trip':        { ja: '往復・片道', en: 'Trip type' },
    'form.oneWay':      { ja: '片道', en: 'One way' },
    'form.roundTrip':   { ja: '往復', en: 'Round trip' },
    'form.return':      { ja: '復路の日付', en: 'Return date' },
    // 往復の説明。**判定を出さない理由を、隠さずに書く。**
    'rt.headline':      { ja: '往復の価格を記録しています（買い時の判定はまだ出していません）',
                          en: 'Tracking round-trip prices (no buy/wait call yet)' },
    'rt.why':           { ja: '表示している金額は往復の合計です。買い時の判定は「安くなる時期」と「遅らせたときの値上がり」から出していますが、そのどちらも往復ではまだ実測できていません。片道で分かったことをそのまま当てはめると外す可能性があるため、確かめられるまでは価格と履歴だけをお見せします。2026年9月中旬から順に分かる見込みです。',
                          en: 'Prices shown are the round-trip total. Our buy/wait call comes from when fares dip and how much they rise if you wait — and we have measured neither for round trips yet. Rather than borrow the one-way answer and risk being wrong, we show you the prices and history only. We expect the first measurements from mid-September 2026.' },
    'watch.daysLeft':   { ja: '出発まで {n} 日', en: '{n} days to departure' },
    'watch.share':      { ja: 'この URL を控えておけば、別の端末からも開けます。URL を知っている人は誰でも開けます。',
                          en: 'Keep this URL to open the monitor elsewhere. Anyone with the URL can open it.' },
    'watch.copy':       { ja: 'URL をコピー', en: 'Copy URL' },
    'watch.copied':     { ja: 'コピーしました', en: 'Copied' },
    'watch.delete':     { ja: 'このモニターを削除', en: 'Delete this monitor' },
    'watch.deleteConfirm': { ja: 'このモニターを削除します。よろしいですか？',
                             en: 'Delete this monitor. Are you sure?' },

    // ── サマリーカード ──
    'card.bestPrice':   { ja: '最安値', en: 'Best price' },
    'card.bestEstimate':{ ja: '予測の底値', en: 'Predicted low' },
    'card.avgPrice':    { ja: '平均価格', en: 'Avg price' },
    'card.avgEstimate': { ja: '予測の平均', en: 'Forecast avg' },
    'card.monitorDays': { ja: 'モニター日数', en: 'Days monitored' },
    'card.nextCheck':   { ja: '次回チェック', en: 'Next check' },
    'card.records':     { ja: '{n} 件の実測', en: '{n} records' },
    'card.noRecords':   { ja: '実測はまだありません', en: 'No real data yet' },
    // **「まだ」と言わない。** 便が無い区間で「まだありません」と出すと、
    // いずれ埋まるように読める。埋まらないことが分かっている。
    'card.noFlights':   { ja: '便が見つかりません', en: 'No flights found' },
    'card.overWindow':  { ja: '出発日までの予測', en: 'through departure' },
    'card.dailyAt':     { ja: '毎日 {t}（{tz}）', en: 'daily at {t} ({tz})' },
    'card.manual':      { ja: '自動収集はオフです', en: 'Auto-collect is off' },

    // ── ダッシュボード ──
    'dash.routeBest':   { ja: '🛫 ルート別最安値', en: '🛫 Best price by route' },
    'dash.top10':       { ja: '🏆 最安フライト TOP10', en: '🏆 Cheapest flights TOP 10' },
    'dash.forecast':    { ja: '📉 予測価格の推移', en: '📉 Price forecast' },
    'dash.forecastSub': { ja: 'モデル推定。実測が入ると置き換わります',
                          en: 'Model estimate. Replaced once real prices arrive.' },
    'dash.asOf':        { ja: '{d} 時点の実測', en: 'observed on {d}' },
    'dash.noPrice':     { ja: '--', en: '--' },

    'th.rank':          { ja: '順位', en: 'Rank' },
    'th.date':          { ja: '日付', en: 'Date' },
    'th.route':         { ja: 'ルート', en: 'Route' },
    'th.airline':       { ja: '航空会社', en: 'Airline' },
    'th.duration':      { ja: '所要', en: 'Duration' },
    'th.price':         { ja: '価格', en: 'Price' },
    'th.band':          { ja: '想定レンジ', en: 'Range' },
    'th.via':           { ja: '経由', en: 'Via' },
    'th.status':        { ja: 'ステータス', en: 'Status' },
    // **「底値帯」という言い方をやめた。** 2026-08-15 の実測で、旧モデルの
    // 45〜75日という窓が広すぎたと分かったため（それ以前と差が出ない）。
    // 谷そのものは 50〜59日前に -4.2% で実在する。ただし浅く、遅延の罰
    // (+16.7%) の4分の1しかないので、待つ理由を与える言い方はしない。
    'status.flat':      { ja: '📉 横ばい', en: '📉 Flat' },
    'status.rising':    { ja: '⚠️ 上昇局面', en: '⚠️ Rising' },
    'status.monitoring':{ ja: '📊 モニター中', en: '📊 Monitoring' },

    // ── 判定 ──
    'verdict.buy_now':  { ja: '今買う', en: 'Buy now' },
    'verdict.wait':     { ja: '待つ', en: 'Wait' },
    'verdict.watch':    { ja: '様子見', en: 'Keep watching' },
    'verdict.insufficient_data': { ja: '実測待ち', en: 'Awaiting real data' },
    'verdict.buy_now.why': { ja: '待っても、期待値として今より安くなりません。',
                             en: 'Waiting is not expected to get you a better price.' },
    // **「安くなる」と言い切らない。** 待って得られる見込みは数%で、
    // その根拠（区間の最安値が50〜60日前にやや安い）は便ごとに測り直せて
    // いない。一方、値上がり開始日を過ぎたときの損失（平均17%）は
    // はるかに大きく確度も高い。**両方を並べて利用者に決めてもらう。**
    'verdict.wait.why':    { ja: 'モデルの見込みでは、今より少し安くなる余地があります。ただし見込める幅は数%で、確かめきれていません。下の「値上がりが始まる日」までに決めてください。それを過ぎると平均17%高くつきます。',
                             en: 'Our model sees some room for the price to fall \u2014 but only by a few percent, and we have not been able to confirm it. Decide before the date shown below: past it, fares run about 17% higher on average.' },
    'verdict.watch.why':   { ja: '安くなる余地はありますが、まだ確信が持てません。値上がりが始まる日までに決めてください。',
                             en: 'It may get cheaper, but we are not confident. Decide before prices start climbing.' },
    'verdict.insufficient_data.why': {
      ja: 'この出発日の実売価格をまだ1件も取得していないため、買い時の判定はできません（比べる相手がいません）。下は推定値です。',
      en: 'No real fares for this departure date yet, so we cannot judge timing — there is nothing to compare against. Estimates below.' },

    'label.observedDays': { ja: '実測 {n} 日分', en: '{n} days of real data' },
    'label.currentBest':  { ja: '今の最安', en: 'Best today' },
    'label.lowestSeen':   { ja: '過去最安（記録）', en: 'Lowest seen (record)' },
    // **「予測の底値」は検証中であることを添える。** 2026-08-24 の突き合わせで、
    // 出発30日以内の予測は平均 +12.7% 高く、信頼帯に入るのは 57% だった。
    'label.predictedMin': { ja: '予測の底値（検証中）', en: 'Predicted low (unverified)' },
    'label.predictedNote': {
      ja: 'この金額はモデルの推定です。出発が近い時期ほど外れやすく、直近の検証では信頼帯に入ったのは約6割でした。確かなのは下の「値上がりが始まる日」のほうです。',
      en: 'This figure is a model estimate. It is least reliable close to departure \u2014 in our latest check the actual price fell inside the band about 60% of the time. The date prices start climbing, below, is the firmer number.' },
    'label.recommended':  { ja: '推奨購入日', en: 'Recommended buy date' },
    'label.asOf':         { ja: '{d} 時点', en: 'as of {d}' },
    'label.record':       { ja: '{d} の記録', en: 'recorded {d}' },
    'label.around':       { ja: '{d} ごろ', en: 'around {d}' },
    'label.nonstop':      { ja: '直行', en: 'Nonstop' },
    'label.via':          { ja: '{a} 経由', en: 'via {a}' },
    'label.oneStop':      { ja: '1経由', en: '1 stop' },
    'label.modelAirline': { ja: 'モデル推定', en: 'Model estimate' },

    'rise.title':    { ja: '値上がりが始まる日', en: 'Prices start rising' },
    'rise.value':    { ja: '{d}（あと {n} 日）', en: '{d} — in {n} days' },
    'rise.valuePast':{ ja: '{d}（すでに上昇局面）', en: '{d} — already rising' },
    'rise.note':     { ja: '出発 {a} 日前。ここから先は出発日に近づくほど上がり、出発30日前を過ぎると平均 {pct}% 高くなります。',
                       en: '{a} days before departure. Prices climb from here, and average {pct}% higher once you are inside 30 days.' },

    // ── 近接空港 ──
    'route.unconfirmed': {
      ja: 'この区間はまだ一度も価格を取得していません。<strong>予想価格は表示しません</strong> — この区間に便があるかどうかも、まだ確かめられていないためです。最初の取得は毎日 03:00（太平洋時間）の収集で行います。便が見つかれば実測が入り、3日続けて1件も見つからない場合は運航されていない可能性が高いものとしてお知らせします。',
      en: 'We have not collected a single price for this leg yet. <strong>We are not showing an estimate</strong> \u2014 we have not even confirmed that this leg is operated. The first attempt runs with the daily 03:00 Pacific collection. If flights turn up you will see real prices; if nothing turns up for three days running, we will tell you the leg is most likely not operated.' },
    'card.unconfirmed': { ja: 'まだ取得していません', en: 'Not collected yet' },
    'route.noFlights': {
      ja: 'この区間の便が見つかりません。3日続けて検索しましたが、直行便も1回乗り継ぎも1件も返りませんでした。<strong>この区間は運航されていない可能性が高いです。</strong>推定価格は表示しません（存在しない便の値段になるため）。別の空港の組み合わせをお試しください。',
      en: 'We cannot find any flights on this leg. We searched for three days running and neither non-stop nor one-stop itineraries returned anything. <strong>This leg is most likely not operated.</strong> We are not showing an estimated price, because it would be a price for a flight that does not exist. Try a different pair of airports.' },
    'route.stopped': {
      ja: 'この区間は収集を止めています。取得を試みても便が返ってこない状態が続いたためです。他の区間は通常どおり収集しています。',
      en: 'Collection is paused for this leg — repeated attempts returned no flights. The other legs are unaffected.' },
    'nearby.badge':     { ja: '近接空港', en: 'Nearby airport' },
    'nearby.noteArrival':   { ja: '{a} 着の便です。{a} から先は陸路などで移動する前提です（その区間の航空券は含みません）。',
                              en: 'Arrives at {a}. You would travel onward by land — that leg is not included.' },
    'nearby.noteDeparture': { ja: '{a} 発の便です。{a} までは陸路などで移動する前提です（その区間の航空券は含みません）。',
                              en: 'Departs from {a}. You would travel there by land — that leg is not included.' },
    'nearby.noteBoth':      { ja: '{o} 発・{d} 着の便です。両端とも陸路などで移動する前提です。',
                              en: 'Departs from {o} and arrives at {d}. Both ends assume land travel.' },
    'nearby.cheaper':   { ja: '{a} 着なら ${n} 安くなります', en: 'Flying into {a} saves ${n}' },

    // ── モデル推定 ──
    'model.badge':      { ja: 'モデル推定', en: 'Model estimate' },
    'model.notice':     { ja: 'まだ実売価格を取得していないため、表示はすべて航空会社のレベニューマネジメントを模したモデルによる推定です。航空会社名は出していません（その会社がこの路線を飛んでいるとは限らないため）。日次収集が走ると実測に置き換わります。',
                          en: 'No real fares yet, so everything shown is an estimate from a model of airline revenue management. Airline names are omitted (a carrier may not serve this route). Real prices replace this after the first collection.' },
    'transferred.badge':  { ja: '推定（別日程の実測から）', en: 'Estimated from other dates' },
    'transferred.notice': { ja: 'この出発日の実売価格はまだ取得していません。表示している便と価格は、同じ路線の別の出発日で実際に観測された {n} 件（最終 {d}）をもとに、各便ごとに較正した推定です。実際に飛んでいることが確認できた航空会社だけを載せています。',
                            en: 'No fares collected for this departure date yet. The flights and prices below are estimates calibrated per itinerary from {n} real observations on other departure dates for this route (last seen {d}). Only carriers actually seen on this route are listed.' },
    'transferred.airlineNote': { ja: '別日程の実測 {n} 件から', en: 'from {n} observations on other dates' },

    // ── グラフ ──
    'chart.observed':   { ja: '実測', en: 'Observed' },
    'chart.projected':  { ja: '予測', en: 'Forecast' },
    'chart.estimate':   { ja: 'モデル推定', en: 'Model estimate' },
    'chart.rising': { ja: 'ここから上がる', en: 'Rising from here' },
    'rise.ahead': {
      ja: '{d}（あと {n} 日）から、この区間で買える最安値が上がりはじめます。安い便から売り切れるためで、いま追いかけている便そのものの値段は、その後もあまり動きません。',
      en: 'From {d}, in {n} days, the cheapest fare available on this route starts climbing \u2014 the cheap flights sell out first. The flight you are following will most likely keep its own price.' },
    'rise.now': {
      ja: 'すでに、この区間で買える最安値が上がる局面に入っています。出発30日前を過ぎると平均 {pct}% 高くなります。ただしこれは「安い便から売り切れる」ことによるもので、いま追いかけている便そのものの値段は、その後もあまり動きません。',
      en: 'The cheapest fare available on this route is already in its climbing phase, averaging {pct}% higher once you are inside 30 days. That is cheap flights selling out, though \u2014 the flight you are following will most likely keep its own price.' },
    'rise.basis': {
      ja: '監視中の全区間の実測から引き直した価格曲線によります。出発30日前を過ぎると、その区間で買える最安値は平均17%上がります（212系列・13,004件）。数%を狙って待つより、遅らせないほうが効きます。 一方、同じ便を追いかけた場合の値段は、出発直前でもほとんど動きません（3,952便の実測、2026年8月に測り直し）。グラフの予測線が平坦なのはそのためです。上がるのは「その区間で買える中でいちばん安いもの」であって、特定の便ではありません。',
      en: 'Based on a price curve refitted from observations across every monitored leg. Once you are inside 30 days, the cheapest fare available on a route runs about 17% higher on average (13,004 points, 212 series) \u2014 so avoiding a late booking matters more than chasing a few percent. Follow one specific flight, though, and its own price barely moves, even close to departure (3,952 flights, remeasured August 2026). That is why the projected line is flat. What climbs is the cheapest option still on sale, not any particular flight.' },
    'settings.currency': { ja: '表示している通貨', en: 'Currency shown' },
    'fx.usdOnly': {
      ja: '価格はすべて米ドル（USD）です。運賃の取得元が米ドル建てで返すため、収集も予測もドルのまま行っています。',
      en: 'All prices are in US dollars (USD). Fares are collected in USD from the source, and both collection and forecasting stay in USD.' },
    'fx.note': {
      ja: '価格は日本円（JPY）で表示しています。1 USD = ¥{rate}（{date} 時点）で換算。運賃の取得と予測はドルで行い、表示のときだけ円にしています。全期間を同じレートで換算しているため、グラフの動きは為替ではなく運賃の動きです。',
      en: 'Prices are shown in Japanese yen (JPY), converted at 1 USD = ¥{rate} as of {date}. Fares are collected and forecast in USD; conversion happens only for display. The whole period uses one rate, so movement in the chart is the fare moving, not the exchange rate.' },
    'chart.followLabel': { ja: '追いかける便', en: 'Following' },
    'chart.observedDays': { ja: '実測{n}日', en: '{n}d observed' },
    'chart.perFlightNote': {
      ja: 'グラフは選んだ便1つを追っています。最安の便は日によって入れ替わるため、日ごとの最安をつないだ線には便の差し替えが混ざり、実際の値動きより大きくぶれます。薄い線は同じ区間の他の便です。',
      en: 'The chart follows one flight. The cheapest flight changes from day to day, so a line joining daily minimums mixes in substitutions and overstates the movement. Faint lines are the other flights on this leg.' },
    'chart.band':       { ja: '信頼帯', en: 'Confidence band' },
    'chart.bandNote':   { ja: '帯の広さがそのまま不確かさです。実測日数が増えるほど狭くなります。',
                          en: 'The band is the uncertainty. It narrows as more real data accumulates.' },

    // ── 設定 ──
    'settings.display':   { ja: '表示', en: 'Display' },
    'settings.nearby':    { ja: '近接空港ルール', en: 'Nearby airports' },
    'settings.nearbyLabel': { ja: '近くの空港も一緒に監視する（推奨）',
                              en: 'Also monitor nearby airports' },
    'settings.nearbyNote': {
      ja: '入れ替え可能な空港（東京の HND/NRT、南カリフォルニアの SAN/LAX）の組み合わせも監視します。既定ではオフです。実測では、東京→南カリフォルニアで HND→SAN が US$1,052 だったのに対し NRT→LAX は US$473 でした。近接空港どうしを結ぶ区間の航空券は含みません（陸路などで移動する前提）。',
      en: 'Also monitors interchangeable airports (HND/NRT for Tokyo, SAN/LAX for Southern California). Off by default. In one measurement, Tokyo to Southern California was US$1,052 via HND-SAN but US$473 via NRT-LAX. Flights between the nearby pair itself are not included — you would travel that leg by land.' },
    'settings.nearbyRoutes': { ja: '監視中: {routes}', en: 'Monitoring: {routes}' },
    'settings.saving':    { ja: '保存中…', en: 'Saving…' },
    'settings.maxDuration':  { ja: '所要時間の上限', en: 'Maximum trip duration' },
    'settings.maxDurationNote': {
      ja: '総所要（飛行時間 + 乗継時間）がこれを超える便を、グラフ・最安値・買い時判定のすべてから外します。乗継が一晩ある便は最安でも候補になりにくいためです。',
      en: 'Flights whose total duration (flying + layover) exceeds this are removed from the chart, the best price and the buy-timing advice. An overnight layover rarely makes a fare worth taking.',
    },
    'settings.noLimit':      { ja: '上限なし', en: 'No limit' },
    'settings.durationExcluded': {
      ja: 'いまの上限で {n} 件を除外しています。',
      en: '{n} itineraries are currently hidden by this limit.',
    },
    'settings.durationNone': {
      ja: 'いまの上限で除外されている便はありません。',
      en: 'No itineraries are hidden by this limit.',
    },
    'settings.airlineFilter': { ja: 'モニターする航空会社', en: 'Airlines to monitor' },
    'settings.airlineFilterNote': {
      ja: '選んだ航空会社だけを、グラフ・最安値・買い時判定の対象にします。何も選ばなければ全社が対象です。',
      en: 'Only the selected airlines are used for the chart, best price and buy timing. Select none to include all.' },
    'filter.selectAll':   { ja: 'すべて選ぶ', en: 'Select all' },
    'filter.clear':       { ja: '選択を解除', en: 'Clear' },
    'filter.save':        { ja: '保存', en: 'Save' },
    'filter.saving':      { ja: '保存中…', en: 'Saving…' },
    'filter.saved':       { ja: '保存しました', en: 'Saved' },
    'filter.selected':    { ja: '{n} 社を選択中', en: '{n} selected' },
    'filter.allSelected': { ja: '全社が対象です', en: 'All airlines included' },
    'filter.empty':       { ja: 'まだ航空会社を観測していません。最初の収集が終わると選べるようになります。',
                            en: 'No airlines observed yet. Choices appear after the first collection.' },
    'filter.notSeen':     { ja: '未観測', en: 'not seen' },
    'filter.noMatch':     { ja: '選択した航空会社の便は、この区間では見つかっていません。',
                            en: 'No flights by the selected airlines have been seen on this leg.' },
    'settings.share':     { ja: '共有', en: 'Share' },
    'settings.sysInfo':   { ja: 'システム情報', en: 'System info' },
    'settings.danger':    { ja: 'モニターの削除', en: 'Delete monitor' },
    'settings.deleteNote':{ ja: '削除すると、この URL を知っている他の人も開けなくなります。収集済みの価格履歴は残ります。',
                            en: 'Deleting makes the URL unusable for everyone. Collected price history is kept.' },

    'info.records':       { ja: '実測レコード数', en: 'Records collected' },
    'info.lastCollection':{ ja: '最終収集', en: 'Last collection' },
    'info.nextCollection':{ ja: '次回収集', en: 'Next collection' },
    'info.schedule':      { ja: 'スケジュール', en: 'Schedule' },
    'info.created':       { ja: '作成日', en: 'Created' },
    'info.expires':       { ja: '有効期限', en: 'Expires' },
    'info.none':          { ja: '（なし）', en: '(none)' },

    'changelog.intro':    { ja: 'このアプリの変更点です。新しいものが上にあります。',
                            en: 'What has changed in this app. Newest first.' },

    'error.generic':      { ja: 'エラーが発生しました', en: 'Something went wrong' },
  },

  t(key, params) {
    const entry = this.dict[key];
    if (!entry) return key;
    let text = entry[this._lang] || entry.ja || key;
    if (params) {
      Object.entries(params).forEach(([k, v]) => {
        text = text.replaceAll(`{${k}}`, v);
      });
    }
    return text;
  },

  setLang(lang) {
    this._lang = lang;
    localStorage.setItem('fppLang', lang);
    document.documentElement.lang = lang;
    this.applyAll();
    document.dispatchEvent(new CustomEvent('langchange'));
  },

  toggle() { this.setLang(this._lang === 'ja' ? 'en' : 'ja'); },

  applyAll() {
    document.querySelectorAll('[data-i18n]').forEach((el) => {
      el.textContent = this.t(el.getAttribute('data-i18n'));
    });
    const btn = document.getElementById('langToggle');
    if (btn) btn.textContent = this.t('lang.toggle');
  },

  init() {
    // **検出結果は保存しない。** 保存すると「明示的に選んだ」と区別が
    // つかなくなり、あとでブラウザ設定を変えても追随できなくなる。
    // 保存するのは setLang（＝利用者が切り替えたとき）だけ。
    document.documentElement.lang = this._lang;
    this.applyAll();
    const btn = document.getElementById('langToggle');
    if (btn) btn.addEventListener('click', () => this.toggle());
  },
};

window.I18N = I18N;
