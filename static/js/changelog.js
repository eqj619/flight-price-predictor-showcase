/**
 * 更新履歴（エンドユーザー向け）。
 *
 * **利用者に見える変更を入れたら、指示が無くてもここに1行足す。**
 * 後追いの別作業にせず、その変更と同じコミット・同じ配備で出す。
 *
 * 判断は1つの問いで決まる — 「利用者が見るもの、できることが変わったか」。
 *   書く   … 機能の追加・変更・削除、画面に出る情報や既定の挙動、対応範囲
 *   書かない … 内部の不具合修正、リファクタリング、運用・配備・監視の変更、
 *              運用者しか使わない CLI、既に書いた項目の手直し
 *
 * 1機能 = 1行。コミット単位で分けない。同じ機能の修正や位置調整を別項目に
 * すると、利用者には何が変わったのか読みにくくなる。
 *
 * 新しいものを配列の先頭に足す。画面もその順で出る。日付は利用者に届く日（PT）。
 * 文言は日英を必ず両方書く。片方だけだと言語を切り替えたときに欠ける。
 */
const CHANGELOG = [
  {
    date: '2026-10-17',
    items: [
      {
        ja: '<strong>出発の当日に「待つ」と表示されることがある問題を直しました。</strong>これから先の最安を探すときに今日の予測まで含めていたため、出発当日でも、今日の予測が実際の価格より低いだけで「待つ」と出ていました。待つのは明日以降のことなので、今日は含めないようにしています。出発日を迎えた便は「今買う」になります。',
        en: '<strong>Fixed “wait” sometimes appearing on the day of departure.</strong> When looking for the cheapest fare still to come, we were including today’s forecast, so on the day of departure a forecast below the actual price was enough to say “wait”. Waiting means buying on a later day, so today is no longer included; on departure day the advice is “buy now”.',
      },
    ],
  },
  {
    date: '2026-10-14',
    items: [
      {
        ja: '<strong>日本発→米国西海岸の便で、「値上がりが始まる日」を出発52日前から65日前に早めました。</strong>出発日を迎えた36便の実際の値動きで確かめたところ、この方向は出発65〜75日前に買うと底値にもっとも近く（底値との差は中央値で+7.5%）、52日前ではすでに上がり始めていました（同+12.5%、底値の5%以内で買えたのは6%）。米国西海岸発→日本の便は、どの日に買っても差が小さかったため、これまでどおり52日前としています。値上がりのお知らせも、この日付で届きます。',
        en: '<strong>For flights from Japan to the US West Coast, the date fares start climbing is now 65 days before departure instead of 52.</strong> Checking 36 flights that have already departed, buying 65–75 days out came closest to the lowest fare (a median of 7.5% above it), while by 52 days out prices had already started to rise (12.5% above, and only 6% of flights were within 5% of the low). For flights from the US West Coast to Japan the day you bought made little difference, so they stay at 52 days. The rising-fares notice now arrives on the new date too.',
      },
    ],
  },
  {
    date: '2026-10-12',
    items: [
      {
        ja: '<strong>価格の取得がときどき空振りし、グラフに抜けが出ていた問題に対処しました。</strong>10月初めから、毎朝の取得の途中で、実際には便がある区間まで「便が見つからない」と返ってくることが増えていました（多い日は2割前後）。同じ検索を時間をおいてやり直すと取れることが分かったので、以前は価格が取れていた区間が空振りしたときは、少し待ってから取り直すようにしています。',
        en: '<strong>Addressed gaps in the price charts caused by fetches that sometimes came back empty.</strong> Since early October, the morning collection increasingly reported no flights partway through — even on routes that plainly have them, on some days around one search in five. The same search run again later does return flights, so when a route that has had prices before comes back empty, we now wait a little and try it once more.',
      },
    ],
  },
  {
    date: '2026-10-11',
    items: [
      {
        ja: '<strong>1年近く先の出発日を指定したモニターが「運航されていない可能性が高い」と誤って表示される問題を直しました。</strong>航空会社が予約を受け付けるのはおおむね出発の330日前からで、それより先の日付は検索しても便が出てきません。これを「便が無い」と取り違え、5日続くと区間の収集を止めていました。予約受付が始まる前の日付は、この判定に数えないようにしています。',
        en: '<strong>Fixed monitors for departures almost a year away being wrongly marked as most likely not operated.</strong> Airlines generally open bookings about 330 days before departure, so searches further out than that return no flights at all. We were reading that as the route not existing, and stopped collecting after five days of it. Dates before bookings open no longer count towards that decision.',
      },
    ],
  },
  {
    date: '2026-09-08',
    items: [
      {
        ja: '<strong>取得できる便のおよそ半分を取りこぼしていた問題を直しました。</strong>価格の取得元は同じ検索の便を2つのまとまりに分けて返しており、こちらは片方しか読んでいませんでした。便がどちらに入るかは日によって変わるため、<strong>同じ便が現れたり消えたりし、価格の推移が穴だらけになっていました</strong>。両方を読むようにし、SAN→HND では1日あたり5件から10件に増えています。安い便が読んでいなかった側に入っていたこともあり、これまでより安い便が見つかる場合があります。ご指摘ありがとうございます。',
        en: '<strong>Fixed a problem where we were missing about half the available flights.</strong> The price source splits one search across two groups of results and we were only reading one of them. Which group a flight lands in changes from day to day, so <strong>the same flight kept appearing and vanishing, leaving gaps in the price history</strong>. We now read both: SAN to Haneda went from 5 flights a day to 10. Some of the cheaper fares were in the half we were not reading, so you may now see lower prices than before. Thanks for the report.',
      },
      {
        ja: '<strong>同じ航空会社・同じ経由地でも、出発時刻の違う便を別々に扱うようにしました。</strong>これまでは1日に何本飛んでいても安い1本しか記録できず、<strong>グラフの1本の線が、日によって中身の違う便につながっていました</strong>。便を選ぶ欄に出発時刻が出るようになり、追いかけている便がその便のまま推移するようになります。',
        en: '<strong>Flights by the same airlines through the same connection are now tracked separately when they depart at different times.</strong> Previously we could only record the cheapest one per day, so <strong>a single line on the chart could be stitched together from different flights</strong>. The flight selector now shows departure times, and the flight you follow stays the same flight.',
      },
      {
        ja: '出発時刻がちょうど正時の便（17:00 など）と深夜0時台の便で、時刻と所要時間が表示されていなかった問題を直しました。所要時間が不明な便は「長さの上限」で除外されないため、<strong>長すぎる便だけが残るという逆の偏りも起きていました</strong>。',
        en: 'Fixed missing departure times and durations for flights leaving exactly on the hour (17:00, say) or in the midnight hour. Flights with an unknown duration are never excluded by your maximum-duration setting, so <strong>this also biased results towards the longest itineraries</strong>.',
      },
      {
        ja: '<strong>値上がりが始まる日のお知らせを、モニターを開いた最初の画面からお申し込みいただけるようにしました。</strong>これまでは「価格トレンド」のタブの中にあり、気づきにくい場所でした。あわせて、<strong>実際に届く通知の文面をその場でお見せします</strong> — 何が届くのか分からないまま許可するのは気持ちの良いものではないためです。お申し込みは押していただいたときだけで、画面を開いただけで許可を求めることはありません。「いいえ」を選ばれた場合は二度とお尋ねしません。',
        en: '<strong>You can now turn on the “fares start climbing” notice from the first screen of a monitor.</strong> It used to sit inside the Price trend tab, where it was easy to miss. We also <strong>show you the exact notice you would receive</strong> — granting permission without knowing what will arrive is an uncomfortable thing to ask. We only request permission when you press the button, never on page load, and if you say no we do not ask again.',
      },
    ],
  },
  {
    date: '2026-09-02',
    items: [
      {
        ja: '日本国内線と韓国路線などで、9月1日から2日間、価格が取得できなくなっていました。取得元が価格を返さない便が検索結果に混ざると、<strong>その区間の便がまるごと失われる</strong>作りになっていたためです。読み取り方を変え、価格のある便だけを拾うようにしました。羽田〜熊本、福岡〜羽田、羽田〜沖縄、成田〜仁川などが復旧しています。ご不便をおかけしました。',
        en: 'For two days from 1 September, prices stopped coming through on Japan domestic and Korea routes, among others. If the source returned even one flight without a price, <strong>every flight on that leg was lost</strong>. We now read the results ourselves and keep the flights that do have a price. Haneda\u2013Kumamoto, Fukuoka\u2013Haneda, Haneda\u2013Okinawa and Narita\u2013Incheon are among those working again. Sorry for the gap.',
      },
    ],
  },
  {
    date: '2026-08-30',
    items: [
      {
        ja: '運航されていない区間に推定価格を表示していた問題を直しました。直行便・1回乗り継ぎとも3日続けて1件も見つからない区間は、<strong>運航されていない可能性が高いものとしてお知らせし、推定価格を表示しません</strong>。これまでは便が0件でも「取得できなかった」場合と区別できず、存在しない便の値段を出し続けていました。ご指摘ありがとうございます。',
        en: 'Fixed a problem where we showed estimated prices for legs that are not operated. When neither non-stop nor one-stop searches return anything for three days running, we now <strong>tell you the leg is most likely not operated and stop showing an estimate</strong>. Until now we could not tell “no flights exist” apart from “the fetch failed”, so we kept quoting prices for flights that do not exist. Thanks for the report.',
      },
      {
        ja: 'あわせて、<strong>モニターを作った直後の予想価格を表示しないことにしました。</strong>価格を一度も取得していない段階では、その区間に便があるかどうかも確かめられていません。そこに値段を出すと、値段が付いていること自体が「この路線はある」という意味になってしまうためです。最初の取得（毎日03:00 太平洋時間）が済むまでは、価格の代わりに状況をお伝えします。',
        en: 'We also <strong>stopped showing an estimated price right after you create a monitor.</strong> Before the first collection we have not confirmed that the leg is operated at all, and putting a price on screen amounts to claiming that it is. Until the first collection runs (03:00 Pacific, daily) we now tell you the status instead of a number.',
      },
    ],
  },
  {
    date: '2026-08-29',
    items: [
      {
        ja: '「価格トレンド」の値上がり開始日の案内から、その日のお知らせを直接受け取れるようにしました。これまで通知の設定は「⚙️ 設定」タブの中だけにあり、気づきにくい場所でした。<br>お送りするのは片道のモニターのみです（往復では値上がりをまだ実測できていないため）。',
        en: 'You can now turn on the price-rise notice directly from the rise date shown under Price Trends. Until now the setting lived only in the Settings tab, where it was easy to miss.<br>One-way monitors only \u2014 we have not yet measured how round-trip fares climb.',
      },
    ],
  },
  {
    date: '2026-08-29',
    items: [
      {
        ja: '一覧の「一覧から外す」を押してもモニターが消えなかった不具合を直しました。ブラウザの中だけで消しており、保存された一覧から外していなかったため、画面を描き直したときに戻ってきていました。ご不便をおかけしました。',
        en: 'Fixed a bug where “Remove from list” did not actually remove a monitor. It was only removed inside your browser, not from your saved list, so it came back as soon as the page redrew. Sorry for the trouble.',
      },
    ],
  },
  {
    date: '2026-08-24',
    items: [
      {
        ja: 'グラフから橙の点線（取得元の履歴）を外しました。8月20日に「作った当日から値動きが見える」ように足したものですが、<strong>グラフは1便を固定して追う作りなので、便を特定しない値を重ねると混乱のもとでした</strong>。航空会社を決めて見ているときに特にそうです。<br>そのぶん、新しく作ったモニターは翌日の収集まで空のままになります。',
        en: 'We have removed the amber dotted line (source history) from the chart. We added it on 20 August so a new monitor would show movement right away, but <strong>the chart follows one specific flight, and overlaying a figure that does not identify a flight was more confusing than helpful</strong> \u2014 especially when you are watching a particular airline.<br>The trade-off: a newly created monitor now stays empty until the next collection run.',
      },
      {
        ja: '「待つ」という判定の書き方を改めました。これまで「待てば安くなる見込みです」とお伝えしていましたが、<strong>言い切れるだけの裏付けがありません</strong>。待って安くなる幅はモデルの見込みでも数%で、その根拠（出発50〜60日前がやや安い）は、いま追いかけている便そのもので確かめられていません。<br>一方、値上がりが始まる日を過ぎたときの上昇（平均17%）は、はるかに確度が高い数字です。両方を並べてお伝えするようにしました。<br>「予測の底値」にも（検証中）と付けました。直近の検証では、出発が近い時期の予測は信頼帯に入ったのが約6割でした。確かなのは「値上がりが始まる日」のほうです。',
        en: 'We have reworded the Wait verdict. It used to say the price was likely to drop; <strong>we cannot support that</strong>. Even by our model the room to fall is only a few percent, and its basis (fares dipping 50\u201360 days out) has not been confirmed for the specific flight you are following.<br>The climb after fares start rising (about 17%) is a much firmer number. We now show both so you can decide.<br>We have also marked Predicted low as unverified: in our latest check, forecasts close to departure landed inside the band only about 60% of the time. The date prices start climbing is the number to trust.',
      },
      {
        ja: '<strong>予測を修正しました。出発直前の値上がりを、大きく見積もりすぎていました。</strong>これまでは「出発が近づくと、追いかけている便の値段が最大2.5倍になる」という想定で予測していましたが、実際にはそうなっていませんでした。同じ便の値段は、出発直前でもほとんど動きません。<br>過去の予測と実測を突き合わせたところ、出発14日以内の予測が平均17%高く出ていました。3,952便の実測から曲線を引き直し、修正しています。<br>「出発30日前を過ぎると平均17%上がる」という説明は変わりません。ただしこれは<strong>その区間で買える最安値</strong>の話で、安い便から売り切れるために起きます。特定の便の値段が上がるという意味ではありませんでした。紛らわしい書き方だったので、こちらも直しました。',
        en: '<strong>We have corrected our forecasts. We were badly overestimating the last-minute climb.</strong> Until now we assumed a flight you are following could reach 2.5x its price as departure neared. It does not: a given flight\u2019s own price barely moves, even close to departure.<br>Checking past forecasts against what actually happened, our predictions inside 14 days of departure ran about 17% high. We have refitted the curve from 3,952 observed flights and corrected it.<br>Our statement that fares run about 17% higher inside 30 days is unchanged \u2014 but that is about <strong>the cheapest fare still available</strong> on a route, which happens because cheap flights sell out. It never meant that a particular flight gets more expensive. We have reworded that too.',
      },
    ],
  },
  {
    date: '2026-08-21',
    items: [
      {
        ja: '通知を受け取れるようにしました。モニターの「設定」タブから有効にできます。<strong>お知らせするのは、監視を始めてから最も安い値を1%以上更新したときだけです。</strong>それ以外では送りません。<br>メールアドレスは受け取りません。通知を届けるために、ブラウザが発行するこの端末向けの文字列だけを保存します。ブラウザの設定でいつでも解除できます。<br>iPhone の場合は、ホーム画面に追加してから有効にしてください。',
        en: 'You can now receive notifications \u2014 turn them on from the Settings tab of a monitor. <strong>We notify you only when the price sets a new low, at least 1% below anything seen since you started watching.</strong> Nothing else.<br>We do not collect your email address. To deliver notifications we store only the identifier your browser issues for this device, and you can revoke it any time in your browser settings.<br>On iPhone, add this site to your Home Screen first.',
      },
      {
        ja: '値上がりが始まる日にもお知らせするようにしました。1件のモニターにつき1回だけです。画面の「買い時分析」に出ている値上がり開始日と同じ日にお送りします。<br>往復のモニターには送りません。往復で運賃がどう上がるかを、まだ実測できていないためです。',
        en: 'We now also notify you on the day fares start climbing \u2014 once per monitor. It arrives on the same date shown as the start of the climb in Buy timing.<br>Round-trip monitors do not get this notice: we have not yet measured how round-trip fares climb.',
      },
      {
        ja: 'iPhone・iPad での手順を画面に出すようにしました。<strong>iOS では、ホーム画面に追加した場合にしか通知を受け取れません。</strong>共有ボタン（□に↑）から「ホーム画面に追加」を選び、追加されたアイコンから開き直してください。あわせてアプリのアイコンを用意したので、ホーム画面と通知にアイコンが出るようになりました。',
        en: 'We now spell out the steps for iPhone and iPad. <strong>On iOS, notifications only reach sites added to the Home Screen.</strong> Tap the Share button, choose \u201cAdd to Home Screen\u201d, then reopen from the new icon. We also added proper app icons, so the Home Screen and your notifications now show one.',
      },
    ],
  },
  {
    date: '2026-08-20',
    items: [
      {
        ja: 'モニターを作った当日から価格の推移が見えるようになりました。取得元が持っている過去約60日ぶんの最安値を、橙の点線としてグラフに重ねます。これまでは収集が1日1回のため、新しいモニターは翌日まで何も表示されませんでした。<br>この線は<strong>便を特定しない値</strong>で、緑の実測（便を固定して追ったもの）とは別の量です。便の入れ替わりを含むぶん実際の値動きより大きく振れるため、予測や買い時の判定には使っていません。',
        en: 'New monitors now show price movement from the moment you create them. We overlay the cheapest fare the source had on record for roughly the past 60 days as an amber dotted line. Until now, because collection runs once a day, a new monitor showed nothing until the following day.<br>This line does <strong>not</strong> identify a specific flight, so it is a different quantity from the green observations, which follow one flight. It swings wider than real price movement, so we do not use it for predictions or the buy/wait call.',
      },
      {
        ja: '往復モニターで「モデルによる推定を表示しています」と誤った説明が出ていたのを直しました。往復では推定を表示しない方針なので、説明のほうが誤りでした。あわせて、グラフが空になる場合に理由を表示するようにしました。',
        en: 'Fixed an incorrect notice on round-trip monitors that said model estimates were being shown. We deliberately do not show estimates for round trips, so the notice was wrong. Empty charts now explain why they are empty.',
      },
    ],
  },
  {
    date: '2026-08-19',
    items: [
      {
        ja: '往復のモニターを作れるようにしました。モニター作成時に「往復」を選び、復路の日付を指定してください。表示する金額は往復の合計です。<br><strong>往復には買い時の判定をまだ出していません。</strong>判定は「安くなる時期」と「遅らせたときの値上がり」から出していますが、そのどちらも往復ではまだ実測できていないためです。片道で分かったことをそのまま当てはめると外す可能性があるので、確かめられるまでは価格と履歴だけをお見せします。2026年9月中旬から順に分かる見込みです。',
        en: 'You can now create round-trip monitors \u2014 choose \u201cRound trip\u201d and pick a return date. Prices shown are the round-trip total.<br><strong>We are not making a buy/wait call on round trips yet.</strong> That call comes from when fares dip and how much they rise if you wait, and we have measured neither for round trips. Rather than borrow the one-way answer and risk being wrong, we show prices and history only. We expect the first measurements from mid-September 2026.',
      },
    ],
  },
  {
    date: '2026-08-17',
    items: [
      {
        ja: '買い時の説明を訂正しました。8月15日に「待っても安くはなりません」とお伝えしましたが、これは言い過ぎでした。改めて調べたところ、出発50〜60日前は他の時期より約4%安い傾向が確かにあります。ただしその差は小さく、出発30日前を過ぎたときの上昇（平均17%）の4分の1です。数%を狙って待つより遅らせないほうが効く、という結論は変わりません。',
        en: 'We have corrected our buy-timing wording. On 15 August we said that waiting would not make fares cheaper; that was overstated. On closer analysis, fares around 50\u201360 days before departure do run about 4% below other periods. But that gap is small \u2014 a quarter of the 17% rise that follows once you are inside 30 days. Our advice is unchanged: avoiding a late booking beats chasing a few percent.',
      },
    ],
  },
  {
    date: '2026-08-15',
    items: [
      {
        ja: '価格トレンドのグラフの見方を変えました。これまで緑の帯で「底値帯」を示していた場所に、橙の帯で「ここから値上がりが始まる」時期を示します。買い時分析にも、値上がりが始まる日と残り日数を出しています。待つべき時期ではなく、いつまでに買うかをお伝えする形にしました。',
        en: 'The price chart now marks when prices start rising, shaded in amber, instead of a green “best window” to wait for. Buy timing also shows that date and the days remaining. The advice is now a deadline rather than a window to wait for.',
      },
      {
        ja: '買い時の判断を、実際の値動きから引き直しました。監視中の全区間について過去約2か月ぶんの価格（212系列・13,004件）を調べたところ、これまで「買い時」としてきた出発45〜75日前に、それより早い時期と比べた安さは見つかりませんでした。一方、出発30日前を過ぎると平均16.7%高くなります。このため、早い時期に「待ちましょう」とご案内することをやめました。安い時期を狙うより、遅らせないことのほうがずっと効きます。',
        en: 'Buy-timing advice is now derived from actual price movements. We examined about two months of price history for every monitored leg — 13,004 observations across 212 series — and found no discount in the 45–75 days before departure that we had been calling the sweet spot. Prices do climb once you are inside 30 days, by 16.7% on average. So we no longer advise waiting. Avoiding a late booking matters far more than timing the dip.',
      },
      {
        ja: '価格の通貨をはっきり示すようにしました。米ドルは「US$1,043」と表記し、設定タブに表示中の通貨をいつでも確認できる欄を置いています。「$」だけではどこの国のドルか分からない、というご指摘によるものです。',
        en: 'Prices now say which dollar they are: US$1,043 rather than $1,043. Settings always shows the currency in use. Raised by a reader who could not tell which country\u2019s dollar was meant.',
      },
    ],
  },
  {
    date: '2026-08-14',
    items: [
      {
        ja: '初めて開いたときの表示言語を、ブラウザの言語設定から判定するようにしました。これまでは必ず日本語で表示していました。一度でも言語を切り替えた方の設定はそのままです。',
        en: 'The app now picks its initial language from your browser settings. It previously always started in Japanese. If you have already chosen a language, your choice is kept.',
      },
      {
        ja: '「近くの空港も一緒に監視する」の初期設定をオフに変更しました。必要な方は設定タブでオンにしてください。すでにオンにしているモニターはそのままです。実測では、この機能で同じ区間が US$1,052 → US$473 になった例があります。',
        en: 'The nearby-airports option is now off by default; turn it on in Settings if you want it. Monitors that already have it on are unchanged. In one measurement it found the same trip for US$473 instead of US$1,052.',
      },
    ],
  },
  {
    date: '2026-08-13',
    items: [
      {
        ja: '日本語表示のとき、価格を円で表示するようにしました。当日の為替レート（設定タブに表示）で換算しています。運賃の取得と予測はドルのまま行い、表示のときだけ円にしています。',
        en: 'Prices are shown in yen when the language is Japanese, converted at the current rate. Fares are collected and forecast in USD; conversion happens only for display.',
      },
    ],
  },
  {
    date: '2026-08-12',
    items: [
      {
        ja: '価格トレンドのグラフを「便ごと」に変えました。追いかける便を選ぶと、その便の値動きだけを表示します。これまでは日ごとの最安をつないでいたため、最安の便が入れ替わるたびに価格が動いたように見えていました。',
        en: 'The price chart now follows a single flight that you choose. Previously it joined each day\u2019s cheapest fare, so the line jumped whenever a different flight became cheapest.',
      },
      {
        ja: '何度試しても便が返ってこない区間は、収集を止めて画面でお知らせするようにしました。データが来ない理由が分かるようになります。他の区間の収集には影響しません。',
        en: 'Legs that repeatedly return no flights now pause collection, and the screen tells you why. Other legs keep collecting as usual.',
      },
    ],
  },
  {
    date: '2026-08-11',
    items: [
      {
        ja: '買い時分析とダッシュボードでも、便が経由する空港を表示するようにしました（例: SAN-LAX-HND）。表示している価格は経由を含めた通しの運賃です。',
        en: 'Buy Timing and the Dashboard now show connecting airports too (e.g. SAN-LAX-HND). Prices shown are for the whole itinerary, including connections.',
      },
      {
        ja: 'モニターの一覧に専用の URL を発行するようにしました。この URL をブックマークしておけば、ブラウザのデータを消しても、別の端末でも、一覧ごと元に戻せます。新しく作ったモニターは自動でこの一覧に入ります。',
        en: 'Your list of monitors now has its own URL. Bookmark it and your whole list comes back — even after clearing browser data, or on another device. New monitors are added automatically.',
      },
      {
        ja: '価格トレンドのグラフに「底値帯」を表示するようにしました。いつが買い時かがグラフ上で分かり、いまが底値帯の前・中・後のどれかも示します。',
        en: 'The price chart now highlights the best window to buy, and tells you whether you are before, inside or past it.',
      },
      {
        ja: '空港の入力に候補表示を追加しました。コード・都市名・空港名のほか、日本語（「東京」「成田」など）でも探せます。存在しない空港コードは登録時にお知らせします。',
        en: 'Airport fields now suggest matches as you type — by code, city or airport name. Codes that are not real airports are rejected with an explanation.',
      },
    ],
  },
  {
    date: '2026-08-10',
    items: [
      {
        ja: '最安フライト一覧に「所要」列を追加しました。あわせて、総所要時間（飛行 + 乗継）が 24 時間を超える便を既定で除外します。設定タブで変更・解除できます。',
        en: 'Added a Duration column, and itineraries longer than 24 hours in total (flying plus layover) are now excluded by default. You can change or remove the limit in Settings.',
      },
      {
        ja: 'このページ（更新履歴）を追加しました。アプリの変更点を新しい順に確認できます。',
        en: 'Added this What\u2019s new page so you can see recent changes, newest first.',
      },
      {
        ja: 'ヘッダーから、自分の別のモニターへ直接切り替えられるようにしました。',
        en: 'You can now jump straight to your other monitors from the header.',
      },
      {
        ja: '「＋ 新しいモニター」ボタンを追加しました。モニター画面からそのまま新規登録に進めます。',
        en: 'Added a “+ New monitor” button so you can create one without going back manually.',
      },
      {
        ja: '最安フライト一覧のルート表記に、経由する空港を含めました（例: NRT-SFO-SAN）。',
        en: 'The route column now shows connecting airports (e.g. NRT-SFO-SAN).',
      },
    ],
  },
  {
    date: '2026-08-09',
    items: [
      {
        ja: '監視できる区間を、米国西海岸の主要5都市（SAN / LAX / SFO / SEA / PDX）と東京（HND / NRT）の間に広げました。',
        en: 'Expanded coverage to five US West Coast cities (SAN / LAX / SFO / SEA / PDX) and Tokyo (HND / NRT).',
      },
      {
        ja: 'モニターが出発日まで続くようになりました。以前はしばらく開かないでいると、出発日より前に価格の取得が止まることがありました。',
        en: 'Monitors now keep collecting until the departure date. Previously they could stop early if you did not open them for a while.',
      },
      {
        ja: '近くの空港もまとめて監視するようになりました（東京の HND / NRT、南カリフォルニアの SAN / LAX）。設定タブでオン・オフできます。',
        en: 'Nearby airports are now monitored together (HND / NRT for Tokyo, SAN / LAX for Southern California). You can turn this off in Settings.',
      },
      {
        ja: 'モニターする航空会社を複数選べるようにしました。選んだ航空会社だけがグラフ・最安値・買い時判定の対象になります。',
        en: 'You can now select multiple airlines to monitor. Only the selected ones feed the chart, best price and buy timing.',
      },
      {
        ja: 'まだ価格を取得していない出発日でも、予測価格を表示するようにしました。実測にもとづく値か推定値かは、画面上で必ず区別しています。',
        en: 'Estimated prices now appear even before any fares have been collected. The screen always distinguishes real observations from estimates.',
      },
      {
        ja: '画面をダッシュボード / 価格トレンド / 買い時分析 / 設定 に整理しました。',
        en: 'Reorganised the app into Dashboard, Price Trends, Buy Timing and Settings.',
      },
      {
        ja: 'サービスを公開しました。',
        en: 'Service launched.',
      },
    ],
  },
];

window.CHANGELOG = CHANGELOG;
