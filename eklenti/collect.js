// Eklenti düğmesine basıldığında açık sayfada çalışır; gönderinin videosunu, açıklamasını
// ve yorumlarını üç yoldan toplar ve her adımın sonucunu tanı listesine yazar.
(async () => {
  const X = window.__vidnoteExtract;
  const opts = Object.assign({ limit: 50, keepUsers: false }, window.__vidnoteOpts || {});
  const diag = [];
  const note = (step, ok, detail) => diag.push({ step, ok, detail: String(detail || '') });
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const HEAD = { 'X-IG-App-ID': '936619743392459', 'X-Requested-With': 'XMLHttpRequest' };

  const code = X.codeFromPath(location.pathname);
  if (!code) return { ok: false, error: 'Bu sayfa bir Reels ya da gönderi sayfası değil. Gönderiyi açıp yeniden dene.', url: location.href, diag };
  const pk = X.codeToPk(code);
  const url = location.origin + '/' + (/\/p\//.test(location.pathname) ? 'p' : 'reel') + '/' + code + '/';

  let media = null;
  const comments = new Map();
  const better = (a, b) => (!a ? b : (!b ? a : Object.assign({}, b, Object.fromEntries(Object.entries(a).filter(([, v]) => v !== '' && v !== null)))));
  const take = (found, fromThisPost) => {
    for (const m of found.media) if (m.code === code || (pk && m.pk === pk)) media = better(m, media);
    for (const c of found.comments) if (c.media_pk ? c.media_pk === pk : fromThisPost) comments.set(c.id, c);
  };

  // 1) Sayfanın kendi yüklediği yanıtlar (hook.js'in tuttukları)
  const store = window.__vidnote;
  if (store) {
    const m = store.media.get(pk) || [...store.media.values()].find(x => x.code === code);
    if (m) media = m;
    const bucket = store.comments.get(pk);
    if (bucket) for (const c of bucket.values()) comments.set(c.id, c);
    note('Sayfanın yüklediği veriler', !!m, `gönderi: ${m ? 'var' : 'yok'}, yorum: ${bucket ? bucket.size : 0}, dinlenen yanıt: ${store.responses}`);
  } else note('Sayfanın yüklediği veriler', false, 'dinleyici yok; eklentiyi kurduktan sonra sayfayı yenile');

  // 2) Sayfanın içine gömülü veriler
  if (!media || !media.videos.length) {
    let n = 0;
    for (const s of document.querySelectorAll('script[type="application/json"]')) {
      const t = s.textContent || '';
      if (t.length > 8e6 || (t.indexOf('video_versions') < 0 && t.indexOf('video_url') < 0)) continue;
      try { take(X.scan(JSON.parse(t)), false); n++; } catch (e) {}
    }
    note('Sayfaya gömülü veriler', !!(media && media.videos.length), `${n} blok tarandı`);
  }

  // 3) Instagram'ın gönderi bilgisi adresi
  if ((!media || !media.videos.length) && pk) {
    try {
      const r = await fetch(`/api/v1/media/${pk}/info/`, { headers: HEAD, credentials: 'include' });
      if (r.ok) take(X.scan(await r.json()), false);
      note('Gönderi bilgisi isteği', !!(media && media.videos.length), `HTTP ${r.status}`);
    } catch (e) { note('Gönderi bilgisi isteği', false, e.message); }
  }

  // 4) Son çare: sayfadaki görünür bilgiler
  if (!media) media = { pk, code, videos: [], caption: '', uploader: '', uploader_name: '', owner_pk: '', taken_at: null, likes: null, comments: null, plays: null };
  if (!media.videos.length) {
    const v = document.querySelector('video');
    const src = v && (v.currentSrc || v.src);
    if (src && /^https:/.test(src)) media.videos = [{ url: src, width: v.videoWidth, height: v.videoHeight }];
    note('Sayfadaki video öğesi', media.videos.length > 0, src ? (/^blob:/.test(src) ? 'adres tarayıcı içi (blob); doğrudan indirilemez' : 'bulundu') : 'video öğesi yok');
  }
  if (!media.caption) {
    const og = document.querySelector('meta[property="og:description"], meta[name="description"]');
    if (og && og.content) { media.caption = og.content; note('Açıklama (sayfa başlığından)', true, 'kısaltılmış olabilir'); }
  }

  // 5) Yorumlar: önce yakalananlar, eksikse Instagram'ın yorum adresi (az sayıda, aralıklı istek)
  let commentPages = 0, commentStatus = '';
  if (opts.limit > 0 && comments.size < opts.limit && pk) {
    let cursor = '';
    for (let page = 0; page < 6 && comments.size < opts.limit; page++) {
      try {
        const q = `/api/v1/media/${pk}/comments/?can_support_threading=true&permalink_enabled=false` + (cursor ? `&min_id=${encodeURIComponent(cursor)}` : '');
        const r = await fetch(q, { headers: HEAD, credentials: 'include' });
        commentStatus = `HTTP ${r.status}`;
        if (!r.ok) break;
        const j = await r.json();
        take(X.scan(j), true);
        commentPages++;
        cursor = j.next_min_id || '';
        if (!cursor) break;
        await sleep(700);
      } catch (e) { commentStatus = e.message; break; }
    }
    note('Yorum isteği', commentPages > 0, `${commentPages} sayfa, ${commentStatus}`);
  }
  note('Yorumlar', comments.size > 0 || opts.limit === 0, opts.limit === 0 ? 'kapalı' : `${comments.size} yorum toplandı`);

  // Yorumları ağaç yap: yanıtlar üst yorumun altına
  const all = [...comments.values()];
  const byId = new Map(all.map(c => [c.id, Object.assign({}, c, { replies: [] })]));
  const top = [];
  for (const c of byId.values()) (c.parent && byId.has(c.parent) ? byId.get(c.parent).replies : top).push(c);
  const clean = c => {
    const o = { text: c.text, likes: c.likes || 0, at: c.at ? new Date(c.at * 1000).toISOString() : '',
                by_owner: !!((media.owner_pk && c.user_pk === media.owner_pk) || (media.uploader && c.user === media.uploader)) };
    if (opts.keepUsers && c.user) o.user = c.user;
    if (c.replies && c.replies.length) o.replies = c.replies.sort((a, b) => a.at - b.at).map(clean);
    return o;
  };
  const list = opts.limit > 0 ? top.slice(0, opts.limit).map(clean) : [];

  media.videos.sort((a, b) => (b.width * b.height) - (a.width * a.height));
  const video = media.videos[0] || null;
  return {
    ok: !!video,
    error: video ? '' : 'Video adresi bulunamadı. Sayfayı yenileyip videoyu bir kez oynattıktan sonra yeniden dene.',
    url, shortcode: code, video,
    uploader: media.uploader, uploader_name: media.uploader_name, caption: media.caption,
    taken_at: media.taken_at ? new Date(media.taken_at * 1000).toISOString() : '',
    stats: { likes: media.likes, comments: media.comments, plays: media.plays },
    comments: list, comments_fetched: list.length, captured_at: new Date().toISOString(), diag,
  };
})();
