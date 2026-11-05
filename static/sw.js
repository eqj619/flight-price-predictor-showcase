/* Service Worker。**ページを閉じていても通知を受け取るために要る。**
 *
 * ここは意図的に薄くしてある。オフラインキャッシュはやらない —
 * 価格は毎日変わるので、古い値を掴んだまま見せるほうが害が大きい。
 * この worker の仕事は「通知を受けて表示する」だけ。
 */

self.addEventListener('install', () => {
  // 古い worker を待たずに差し替える。薄い worker なので安全。
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  // **本文が壊れていても通知は出す。** Chrome は push を受けたら必ず
  // 何かを表示することを要求しており、黙って捨てると購読を取り消される。
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = {};
  }

  const title = payload.title || 'Flight Price Predictor';
  const options = {
    body: payload.body || '',
    icon: '/static/icon-192.png',
    // **badge は icon の使い回しにしない。** Android は badge をアルファ
    // だけ見る白黒のマスクとして扱うので、色付きの画像を渡すと四角い塊に
    // なる。badge-72.png は透過に白い機体だけを置いてある。
    badge: '/static/badge-72.png',
    // 同じモニターの通知は重ねず置き換える。溜まっても読まれない。
    tag: payload.tag || 'fpp',
    renotify: false,
    data: { url: payload.url || '/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  // 既に開いているタブがあればそれを使う。増やさない。
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((list) => {
        for (const client of list) {
          if (client.url.endsWith(url) && 'focus' in client) return client.focus();
        }
        return self.clients.openWindow(url);
      }),
  );
});
