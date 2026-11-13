// Sayfanın kendi ağ yanıtlarını dinler ve içindeki gönderi/yorum kayıtlarını bellekte tutar.
// Böylece Instagram iç adreslerini değiştirse de, sayfanın zaten yüklediği veriden yararlanılır.
// Hiçbir şey dışarı gönderilmez; kayıtlar yalnızca eklenti düğmesine basıldığında okunur.
(() => {
  if (window.__vidnote) return;
  const X = window.__vidnoteExtract;
  const store = { media: new Map(), comments: new Map(), responses: 0 };
  window.__vidnote = store;
  const MAX_MEDIA = 80, MAX_COMMENTS = 3000;

  function remember(found, hint) {
    for (const m of found.media) {
      const key = m.pk || m.code;
      const old = store.media.get(key);
      store.media.delete(key);
      store.media.set(key, old ? Object.assign(old, Object.fromEntries(Object.entries(m).filter(([, v]) => v !== '' && v !== null))) : m);
      if (store.media.size > MAX_MEDIA) store.media.delete(store.media.keys().next().value);
    }
    for (const c of found.comments) {
      const key = c.media_pk || hint;
      if (!key) continue;
      if (!store.comments.has(key)) store.comments.set(key, new Map());
      const bucket = store.comments.get(key);
      if (bucket.size < MAX_COMMENTS) bucket.set(c.id, c);
      if (store.comments.size > MAX_MEDIA) store.comments.delete(store.comments.keys().next().value);
    }
  }

  function ingest(url, body, text) {
    try {
      if (!text || text.length > 12e6 || text.indexOf('{') < 0) return;
      if (text.indexOf('video_versions') < 0 && text.indexOf('"text"') < 0 && text.indexOf('video_url') < 0) return;
      const data = JSON.parse(text.replace(/^for \(;;\);/, ''));
      store.responses++;
      // Bu yanıt hangi gönderiye ait? Adresten, istek gövdesinden ya da açık sayfadan çıkar.
      let hint = (/\/media\/(\d+)[/_]/.exec(url) || [])[1] || '';
      if (!hint && body) {
        const b = (() => { try { return decodeURIComponent(body); } catch (e) { return body; } })();
        hint = (/"media_id"\s*:\s*"?(\d+)/.exec(b) || [])[1] || '';
        if (!hint) { const sc = (/"shortcode"\s*:\s*"([\w-]+)"/.exec(b) || [])[1]; if (sc) hint = X.codeToPk(sc); }
      }
      if (!hint) { const sc = X.codeFromPath(location.pathname); if (sc) hint = X.codeToPk(sc); }
      remember(X.scan(data), hint);
    } catch (e) { /* tanınmayan yanıt: yok say */ }
  }
  store.ingest = ingest;

  const interesting = url => /\/graphql|\/api\/v1\//.test(url);

  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const p = origFetch.apply(this, arguments);
    try {
      const url = String((input && input.url) || input);
      if (interesting(url)) {
        const body = init && typeof init.body === 'string' ? init.body : '';
        p.then(r => r.clone().text()).then(t => ingest(url, body, t)).catch(() => {});
      }
    } catch (e) { /* sayfanın isteğini asla bozma */ }
    return p;
  };

  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    try { this.__vnUrl = String(url); } catch (e) {}
    return open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    try {
      if (this.__vnUrl && interesting(this.__vnUrl)) {
        const b = typeof body === 'string' ? body : '';
        this.addEventListener('load', () => {
          try { if (this.responseType === '' || this.responseType === 'text') ingest(this.__vnUrl, b, this.responseText); } catch (e) {}
        });
      }
    } catch (e) {}
    return send.apply(this, arguments);
  };
})();
