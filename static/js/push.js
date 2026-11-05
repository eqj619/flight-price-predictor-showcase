/* 通知の購読（課題4）。
 *
 * 設計上の要点:
 *
 * - **読み込み時に許可を求めない。** 断られたら二度と出せないので、
 *   ボタンを押されたときだけ subscribe() する
 * - **鍵が設定されていなければ、ボタンごと出さない。** 「通知を受け取る」
 *   と見せて何も来ないのが一番まずい
 * - メールアドレスは受け取らない。保存するのは endpoint だけ
 */
const Push = (() => {
  const supported = 'serviceWorker' in navigator
    && 'PushManager' in window
    && 'Notification' in window;

  // **iPad は自分を Mac だと名乗る。** userAgent だけでは判別できないので、
  // タッチ点数を併せて見る（デスクトップ Safari は maxTouchPoints=0）。
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

  // ホーム画面から開かれているか。iOS は独自の navigator.standalone しか
  // 持たない時期が長かったので、両方見る。
  const isStandalone = window.navigator.standalone === true
    || (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);

  /** 「対応していない」のではなく「ホーム画面に追加すれば使える」状態か。
   *
   * iOS の Safari は通常のタブでは PushManager を持たない。ホーム画面に
   * 追加した単独窓でだけ現れる。**この2つを混ぜて「非対応」と出すと、
   * 使えるはずの利用者を取り逃す。**
   */
  const needsHomeScreen = !supported && isIOS && !isStandalone;

  // iOS では Chrome も Firefox も中身は WebKit で、Safari と同じ制約を
  // 受ける。**ただしホーム画面への追加は Safari から行うのが確実。**
  // 別のブラウザのタブで「ホーム画面に追加してください」とだけ出すと、
  // その手順が見つからないか、見つけても期待どおりにならないことがある。
  // まず Safari で開いてもらう。
  const isIOSNonSafari = isIOS
    && /CriOS|FxiOS|EdgiOS|OPiOS|Chrome/.test(navigator.userAgent);

  /** base64url の公開鍵を、subscribe() が求める Uint8Array にする。 */
  function toKeyBytes(base64) {
    const padded = (base64 + '='.repeat((4 - base64.length % 4) % 4))
      .replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(padded);
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }

  /** PushSubscription から、サーバに渡す3つの値を取り出す。 */
  function unpack(subscription) {
    const json = subscription.toJSON();
    return {
      endpoint: json.endpoint,
      p256dh: json.keys.p256dh,
      auth: json.keys.auth,
    };
  }

  // 公開鍵を先に取っておく。**subscribe() の中で fetch してはいけない。**
  // Safari は Notification.requestPermission() を「利用者の操作と同じ処理の
  // 中」で呼ぶことを求める。間に await が1つでも入ると操作との結び付きが
  // 切れ、許可ダイアログが出ないまま失敗する。画面側が prime() を先に
  // 呼んでおき、押された瞬間は同期的に requestPermission へ入る。
  let cached = null;

  async function config() {
    if (cached) return cached;
    const res = await fetch('/api/push/key');
    cached = res.ok ? await res.json() : { enabled: false };
    return cached;
  }

  async function registration() {
    // scope は '/'。sw.js をルート直下から配っているので通る。
    return navigator.serviceWorker.register('/sw.js', { scope: '/' });
  }

  async function current() {
    if (!supported) return null;
    const reg = await navigator.serviceWorker.getRegistration('/');
    if (!reg) return null;
    return reg.pushManager.getSubscription();
  }

  /** 許可を求めて購読する。**押されたときだけ呼ぶこと。**
   *
   * **最初の1行が requestPermission であること。** ここより前に await を
   * 置くと Safari で許可ダイアログが出なくなる（利用者の操作と結び付いた
   * 処理の中でしか呼べないため）。公開鍵は prime() で先に取ってある。
   */
  async function subscribe(watchToken) {
    if (!supported) throw new Error('unsupported');
    if (!cached || !cached.enabled) throw new Error('disabled');
    const publicKey = cached.public_key;

    const permission = await Notification.requestPermission();
    if (permission !== 'granted') throw new Error(permission);

    const reg = await registration();
    await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        // Chrome はこれを必須にしている。裏で黙って動く用途には使えない。
        userVisibleOnly: true,
        applicationServerKey: toKeyBytes(publicKey),
      });
    }

    const res = await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // 通知の本文はサーバが作るので、いまの表示言語を渡す。
      body: JSON.stringify({
        ...unpack(sub),
        watch_token: watchToken,
        lang: (typeof I18N !== 'undefined' && I18N.lang) || 'en',
      }),
    });
    if (!res.ok) throw new Error('subscribe_failed');
    return res.json();
  }

  /** このモニターの通知だけをやめる。購読そのものは残す。 */
  async function unsubscribe(watchToken) {
    const sub = await current();
    if (!sub) return;
    await fetch('/api/push/unsubscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint: sub.endpoint, watch_token: watchToken }),
    });
  }

  /** この端末が購読しているモニターのトークン。未購読なら空。
   *
   *  **一覧では1回で済ませる。** モニターごとに isSubscribed を呼ぶと
   *  件数だけ問い合わせが飛ぶが、返ってくるのは毎回同じ一式。
   */
  async function subscribedWatches() {
    const sub = await current();
    if (!sub) return [];
    const res = await fetch(
      `/api/push/status?endpoint=${encodeURIComponent(sub.endpoint)}`);
    if (!res.ok) return [];
    const { watches } = await res.json();
    return watches || [];
  }

  /** このモニターを購読済みか。画面のボタンの状態に使う。 */
  async function isSubscribed(watchToken) {
    return (await subscribedWatches()).includes(watchToken);
  }

  /** 通知の誘いを断られたことを覚えておく鍵。
   *
   *  **一度断られたら、どの画面でも二度と出さない。** モニター画面と
   *  一覧で別々に覚えると、片方で断った人に他方がまた尋ねる。しつこく
   *  すると、次に本当に欲しくなったときの手段まで失う（許可はブラウザが
   *  一度拒否されると出し直させてくれない）。**鍵はここに1つだけ置く。**
   */
  const DISMISSED = 'fppNotifyOfferDismissed';
  function offerDismissed() {
    try { return !!localStorage.getItem(DISMISSED); } catch { return false; }
  }
  function dismissOffer() {
    try { localStorage.setItem(DISMISSED, '1'); } catch { /* 出し続けない */ }
  }

  return { supported, needsHomeScreen, isIOS, isIOSNonSafari, isStandalone,
           config, prime: config, subscribe, unsubscribe, isSubscribed,
           subscribedWatches, offerDismissed, dismissOffer };
})();
