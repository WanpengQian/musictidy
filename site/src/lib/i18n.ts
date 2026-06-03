/**
 * 站点 i18n —— Astro 静态生成，按构建期 locale 取 dict。
 *
 * URL 规则（astro.config.mjs 里定的）：
 *   zh: /         (默认)
 *   en: /en/
 *   ja: /ja/
 *
 * 每个页面 frontmatter 通过 Astro.currentLocale 拿当前 locale，传给 t()。
 */

export type Locale = 'zh' | 'en' | 'ja';
export const LOCALES: Locale[] = ['zh', 'en', 'ja'];
export const LOCALE_LABELS: Record<Locale, string> = {
  zh: '简体中文',
  en: 'English',
  ja: '日本語',
};
export const LOCALE_LANG_TAG: Record<Locale, string> = {
  zh: 'zh-Hans',
  en: 'en',
  ja: 'ja',
};
export const LOCALE_FONT: Record<Locale, string> = {
  zh: '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Source Han Sans CN", "Noto Sans SC", system-ui, -apple-system, sans-serif',
  ja: '"Hiragino Kaku Gothic ProN", "Hiragino Sans", "Yu Gothic", "Yu Gothic Medium", Meiryo, "Noto Sans JP", system-ui, -apple-system, sans-serif',
  en: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
};

type Dict = Record<string, string>;

const DICTS: Record<Locale, Dict> = {
  zh: {
    // BaseLayout / nav / footer
    'nav.features': '功能',
    'nav.deploy': '部署',
    'nav.support': 'Support',
    'nav.privacy': '隐私',
    'nav.github': 'GitHub ↗',
    'nav.tryWeb': '在线试用',
    'footer.tagline': 'MusicTidy · 开源 (MIT) · 自托管',
    'footer.privacy': '隐私政策',
    'footer.support': 'Support',
    'footer.github': 'GitHub',
    'meta.titleSuffix': 'MusicTidy — 自托管音乐馆',
    'meta.description':
      '你的音乐，自托管。MusicTidy 把散落的本地音乐整理成一座干净的私有音乐馆，iPhone 上听就跟 Apple Music 一样顺。',

    // Hero
    'hero.headline.line1': '你买的、你扒的、你攒的',
    'hero.headline.line2': '音乐都该是你的',
    'hero.sub':
      '视频满天飞的时代，我只想<strong>好好听首歌</strong>。MusicTidy 把散落几十年的本地音乐文件整理成一座干净的私有音乐馆，iPhone 上配一个<strong>专注、顺手</strong>的 App —— 没视频、没短视频、没 podcast 推流。<strong>免费</strong>开箱即用，Pro 只是锦上添花。',
    'hero.ctaPrimary': '在线试用 →',
    'hero.ctaGhost': '看部署文档',

    // Web app section
    'web.title': '不想装 App？浏览器直接试',
    'web.lead':
      'Web 端就在 <strong>app.musictidy.com</strong>，开浏览器登你自家 MusicTidy server 就能听。手机、电脑、桌面 / 触屏全适配。完全免费，没账号，没广告。',
    'web.cta': '打开 app.musictidy.com →',
    'web.featAlbum.title': '专辑视角',
    'web.featAlbum.desc': '按 MusicBrainz 的曲目表展开，告诉你哪几首在库里，哪几首还缺。',
    'web.featPlaylist.title': '保存为播放列表',
    'web.featPlaylist.desc': '从队列里勾选，一键存到服务器；浏览器换台也还在。',
    'web.featLang.title': '三语 + 字体跟随',
    'web.featLang.desc': '中 / 英 / 日，UI 字体按当前语言切，日语系统看中文也不丑。',
    'web.featLocal.title': '只为你跑',
    'web.featLocal.desc': 'Web 端只是壳，音频流来自你自己的 server，无中转、不分析、不留数据。',

    // 痛点区
    'pain.title': '为什么造这个？',
    'pain.lead':
      '2003 年 Napster 时代攒的 MP3、CD 转的 FLAC、从虾米 / iTunes Store 买的曲子……散落在三块硬盘、几个 NAS、若干网盘里。你想随时听任何一首，但现实是：',
    'pain1.title': '流媒体在删你的歌',
    'pain1.desc':
      'Spotify 下架了某张专辑、Apple Music 改成了 remix 版、订阅到期"我的音乐"全消失。你以为是租，结果连租约都说没就没。',
    'pain2.title': '本地音乐 = 乱',
    'pain2.desc':
      '同一首 6 个版本、文件夹名拼写五花八门、专辑封面散在 zip 里。iTunes 老导库每次都让你怀疑人生。',
    'pain3.title': 'iPhone 不让你听本地',
    'pain3.desc':
      'iPhone 端要么没 app，要么强推视频 / podcast / "为你推荐"。只想纯粹听一首自己拥有的歌，反而最难。',

    // Solution
    'solution.title': '解法：把自己的歌当成自己的',
    'solution.serverTitle': '一台 MusicTidy server',
    'solution.serverDesc':
      '跑在 NAS / 旧 Mac / 小机箱上，认识 MP3 / FLAC / WAV / APE 等本地音乐，自动按 MusicBrainz 元数据梳理成专辑视图。开源（MIT），自托管，不依赖任何第三方账号。',
    'solution.iosTitle': '一个 iPhone App',
    'solution.iosDesc':
      '内网 / Cloudflare Tunnel 接进来，列表、播放、AirPlay、Now Playing 控制中心 —— 全跟 Apple Music 一致。<strong>只听音乐</strong>，没视频、没 podcast 推流、没"为你推荐"。',

    // 三步上手
    'steps.title': '三步上手',
    'steps.s1.title': '装 server',
    'steps.s1.desc':
      '一行 docker 跑起来。指向你音乐文件夹（NAS 上多 TB 也行），首次扫描自动按 MusicBrainz 整理。',
    'steps.s2.title': '装 iPhone App',
    'steps.s2.desc':
      'App Store 下载（即将上架），输入 server 地址，扫码登一下。iPhone 出门就能听你 NAS 上那张 1998 年的盘。',
    'steps.s3.title': '坐下来听',
    'steps.s3.desc':
      '剩下的就是听了。专辑 / 艺人 / 队列三个 tab，AirPlay、车机、控制中心一应俱全。',

    // 截图
    'shots.title': '长这样',

    // CTA / demo / footer
    'demo.title': '在线试用',
    'demo.desc':
      '不想装 server 也能体验：访问 <strong>app.musictidy.com</strong>，用 demo 服务器登（试用按钮里有预设），听一段我们准备的 CC 授权音乐，看看专辑视图 / 播放列表 / 多语言切换怎么用。',
    'demo.go': '打开试用 →',

    'finalCta.title': '准备好把音乐拿回来了？',
    'finalCta.desc':
      'Server 完全开源（MIT），iPhone App 即将上架 App Store。<br/>免费即用，<strong>没订阅、没账号</strong>，更不会偷偷给你推送视频。',
    'finalCta.deploy': '部署文档',
    'finalCta.github': '源码 ↗',
  },

  en: {
    'nav.features': 'Features',
    'nav.deploy': 'Deploy',
    'nav.support': 'Support',
    'nav.privacy': 'Privacy',
    'nav.github': 'GitHub ↗',
    'nav.tryWeb': 'Try web',
    'footer.tagline': 'MusicTidy · open source (MIT) · self-hosted',
    'footer.privacy': 'Privacy',
    'footer.support': 'Support',
    'footer.github': 'GitHub',
    'meta.titleSuffix': 'MusicTidy — your self-hosted music library',
    'meta.description':
      'Your music, self-hosted. MusicTidy turns the local audio you bought, ripped, and collected over the years into a clean private library, with an iPhone app that feels like Apple Music.',

    'hero.headline.line1': 'The music you bought, ripped, collected',
    'hero.headline.line2': 'shouldn’t be rented',
    'hero.sub':
      'In a video-saturated age, all I want is to <strong>just listen to a song</strong>. MusicTidy tidies decades of scattered local audio into a clean private library, with a <strong>focused, polished</strong> iPhone app — no videos, no Reels, no podcast push. <strong>Free</strong> out of the box; Pro is just sugar on top.',
    'hero.ctaPrimary': 'Try online →',
    'hero.ctaGhost': 'Deploy guide',

    'web.title': 'Don’t want to install the app? Try it in your browser',
    'web.lead':
      'The web client lives at <strong>app.musictidy.com</strong>. Open it, point it at your own MusicTidy server, and play. Phone, laptop, desktop — touch or mouse. Free, no account, no ads.',
    'web.cta': 'Open app.musictidy.com →',
    'web.featAlbum.title': 'Album-first',
    'web.featAlbum.desc':
      'Tracks follow the MusicBrainz canonical track list — see exactly what’s owned and what’s missing.',
    'web.featPlaylist.title': 'Save as playlist',
    'web.featPlaylist.desc':
      'Tick tracks from the queue, save once, and the playlist lives on the server — switch browsers freely.',
    'web.featLang.title': '3 languages, native fonts',
    'web.featLang.desc':
      'EN / ZH / JA, UI font switches with locale so JP-on-EN-OS (or vice versa) still looks right.',
    'web.featLocal.title': 'Yours only',
    'web.featLocal.desc':
      'The web app is a shell. Audio streams straight from your server — no proxy, no analytics, no tracking.',

    'pain.title': 'Why this exists',
    'pain.lead':
      'MP3s from the Napster era, FLACs ripped from CDs, tracks bought on iTunes Store or Xiami — scattered across three drives, a couple NAS boxes, and several clouds. You want to hear any of them on demand. The reality:',
    'pain1.title': 'Streaming deletes your music',
    'pain1.desc':
      'An album quietly leaves Spotify. Apple Music swaps in a remix. Your "saved" library evaporates when the subscription lapses. You weren’t buying, you were renting — and the lease can end anytime.',
    'pain2.title': 'Local audio is a mess',
    'pain2.desc':
      'Six versions of the same song, a dozen folder-naming conventions, cover art trapped in zips. iTunes makes you re-import every time and you feel a little less hopeful.',
    'pain3.title': 'iPhone won’t play your local stuff',
    'pain3.desc':
      'Either no app exists, or it shoves videos / podcasts / "For You" at you. The simple act of playing a song you already own becomes the hardest thing on the device.',

    'solution.title': 'The fix: own the playback too',
    'solution.serverTitle': 'A MusicTidy server',
    'solution.serverDesc':
      'Runs on your NAS, an old Mac, or any small box. Reads MP3 / FLAC / WAV / APE and folds them into album views using MusicBrainz metadata. Open source (MIT), self-hosted, no third-party account anywhere.',
    'solution.iosTitle': 'An iPhone app',
    'solution.iosDesc':
      'Reach it over LAN or Cloudflare Tunnel. Browsing, playback, AirPlay, Control Center now-playing — all behave like Apple Music. <strong>Music only</strong>. No videos, no podcasts, no "for you".',

    'steps.title': 'Three steps',
    'steps.s1.title': 'Run the server',
    'steps.s1.desc':
      'One docker line. Point it at your music folder (multi-TB NAS is fine), first scan tidies it via MusicBrainz automatically.',
    'steps.s2.title': 'Install the iPhone app',
    'steps.s2.desc':
      'Download from the App Store (coming soon), enter your server URL, sign in. That 1998 album on your NAS now plays everywhere your iPhone goes.',
    'steps.s3.title': 'Sit down and listen',
    'steps.s3.desc':
      'That’s it. Albums / Artists / Queue. AirPlay, CarPlay, Control Center — all wired up.',

    'shots.title': 'Looks like this',

    'demo.title': 'Try online',
    'demo.desc':
      'Don’t feel like setting up a server first? Open <strong>app.musictidy.com</strong>, tap "Try demo" (preset server in the picker), and listen to a CC-licensed set we prepared. Explore the album view, playlists, and language switcher.',
    'demo.go': 'Open the demo →',

    'finalCta.title': 'Ready to own your music?',
    'finalCta.desc':
      'The server is fully open source (MIT); the iPhone app is coming to the App Store soon.<br/>Free out of the box, <strong>no subscriptions, no accounts</strong>, no surprise videos.',
    'finalCta.deploy': 'Deploy guide',
    'finalCta.github': 'Source ↗',
  },

  ja: {
    'nav.features': '機能',
    'nav.deploy': 'デプロイ',
    'nav.support': 'サポート',
    'nav.privacy': 'プライバシー',
    'nav.github': 'GitHub ↗',
    'nav.tryWeb': 'Web で試す',
    'footer.tagline': 'MusicTidy · オープンソース (MIT) · セルフホスト',
    'footer.privacy': 'プライバシー',
    'footer.support': 'サポート',
    'footer.github': 'GitHub',
    'meta.titleSuffix': 'MusicTidy — セルフホストの音楽ライブラリ',
    'meta.description':
      'あなたの音楽は、あなたのもの。MusicTidy は買った / 取り込んだ / 集めたローカル音源をきれいなプライベートライブラリに整え、iPhone でも Apple Music のように快適に聴けます。',

    'hero.headline.line1': '買った曲、リッピングした曲、集めた曲',
    'hero.headline.line2': '全部あなたのものであるべき',
    'hero.sub':
      '動画ばかりの今、ただ<strong>音楽を一曲ちゃんと聴きたい</strong>だけ。MusicTidy は何十年と散らばったローカル音源を、きれいなプライベートライブラリにまとめます。iPhone 側は<strong>集中して使える専用アプリ</strong>。動画なし、Reels なし、ポッドキャスト誘導なし。<strong>無料</strong>で完結、Pro は飾り。',
    'hero.ctaPrimary': 'オンラインで試す →',
    'hero.ctaGhost': 'デプロイ手順',

    'web.title': 'アプリ入れずに、まずブラウザで',
    'web.lead':
      'Web 版は <strong>app.musictidy.com</strong>。自分の MusicTidy server に向ければそのまま再生できます。スマホ / PC / タッチ / マウス全部対応、無料、アカウント不要、広告なし。',
    'web.cta': 'app.musictidy.com を開く →',
    'web.featAlbum.title': 'アルバム視点',
    'web.featAlbum.desc':
      'MusicBrainz の曲目表に沿って展開。手元に何曲あって、何曲欠けているかが一目瞭然。',
    'web.featPlaylist.title': 'プレイリスト保存',
    'web.featPlaylist.desc':
      'キューからチェックして一発で server に保存。ブラウザを変えても残ります。',
    'web.featLang.title': '3 言語 + フォント自動切替',
    'web.featLang.desc':
      '日 / 英 / 中、UI フォントが言語ごとに切り替わるので日本語 OS で中国語を見ても崩れません。',
    'web.featLocal.title': 'あなただけのために動く',
    'web.featLocal.desc':
      'Web 版は外殻だけ。音はあなたの server から直接流れます。中継なし、解析なし、データ保存なし。',

    'pain.title': 'なぜ作ったか',
    'pain.lead':
      'Napster 時代の MP3、CD から取り込んだ FLAC、iTunes Store や Xiami で買った曲 ── 全部 3 台のドライブと NAS、クラウドにバラバラ。聴きたい時にすぐ聴ける、はずなのに：',
    'pain1.title': 'ストリーミングは曲を消す',
    'pain1.desc':
      'Spotify からアルバムが消える。Apple Music でリミックスに差し替わる。サブスクが切れたら "マイミュージック" まるごと蒸発。買ったつもりが、レンタル、しかも契約はいつ終わるか分からない。',
    'pain2.title': 'ローカル音源 = カオス',
    'pain2.desc':
      '同じ曲が 6 バージョン、フォルダ名は綴りバラバラ、ジャケットは zip の中。iTunes はインポートし直すたび心が折れる。',
    'pain3.title': 'iPhone はローカルを聴かせてくれない',
    'pain3.desc':
      'アプリがそもそもないか、動画 / ポッドキャスト / "おすすめ" を押し付けてくる。自分の持ってる曲を 1 曲鳴らす、それが一番難しい。',

    'solution.title': '解決：再生も自分のものに',
    'solution.serverTitle': 'MusicTidy server',
    'solution.serverDesc':
      'NAS / 古い Mac / 小型機で動かす。MP3 / FLAC / WAV / APE などを読み込み、MusicBrainz メタで自動的にアルバム表示に整えます。オープンソース（MIT）、セルフホスト、サードパーティアカウント一切不要。',
    'solution.iosTitle': 'iPhone アプリ',
    'solution.iosDesc':
      'LAN / Cloudflare Tunnel から接続。一覧・再生・AirPlay・コントロールセンターの再生中表示まで Apple Music と同じ感覚。<strong>音楽だけ</strong>。動画なし、ポッドキャストなし、"おすすめ" なし。',

    'steps.title': '3 ステップ',
    'steps.s1.title': 'server を入れる',
    'steps.s1.desc':
      'docker 1 行で起動。音楽フォルダ（数 TB の NAS でも可）を指定すれば初回スキャンで MusicBrainz 整理が自動進行。',
    'steps.s2.title': 'iPhone アプリを入れる',
    'steps.s2.desc':
      'App Store からダウンロード（近日公開）、server の URL を入れてサインイン。NAS にある 1998 年のアルバムも、iPhone と一緒にどこへでも。',
    'steps.s3.title': 'あとは聴くだけ',
    'steps.s3.desc':
      'アルバム / アーティスト / キュー。AirPlay も CarPlay もコントロールセンターも揃ってます。',

    'shots.title': 'こんな見た目',

    'demo.title': 'オンラインで試す',
    'demo.desc':
      'いきなり server を立てるのが面倒なら <strong>app.musictidy.com</strong> を開いて "デモを試す" を押すだけ。プリセット済みの server に接続して、CC ライセンスの音源でアルバム表示・プレイリスト・多言語切替を実際に触れます。',
    'demo.go': 'デモを開く →',

    'finalCta.title': '音楽を取り戻す準備、できましたか？',
    'finalCta.desc':
      'Server は完全オープンソース（MIT）、iPhone アプリは App Store にもうすぐ。<br/>無料で開けばすぐ使える。<strong>サブスクなし、アカウントなし</strong>、こっそり動画が出てくることもありません。',
    'finalCta.deploy': 'デプロイ手順',
    'finalCta.github': 'ソース ↗',
  },
};

export function t(locale: Locale | string | undefined, key: string): string {
  const l = (locale as Locale) || 'zh';
  const d = DICTS[l] || DICTS.zh;
  return d[key] ?? DICTS.zh[key] ?? key;
}

/** 当前 locale 在另外 locale 下的等价路径。Astro 默认 prefixDefaultLocale=false，
 *  zh 直接 /…，en /en/…，ja /ja/…。当前 URL 已经带 /en|/ja 前缀就先剥掉再拼。 */
export function pathFor(locale: Locale, currentPathname: string): string {
  const stripped = currentPathname.replace(/^\/(en|ja)(\/|$)/, '/');
  if (locale === 'zh') return stripped || '/';
  const base = stripped === '/' ? '' : stripped;
  return `/${locale}${base}` || `/${locale}/`;
}
