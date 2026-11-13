// Instagram'ın kendi yanıtlarındaki (JSON) gönderi ve yorum kayıtlarını tanır.
// Hem sayfanın yaptığı isteklerde (hook.js) hem de tıklama anındaki toplamada (collect.js) kullanılır.
(() => {
  const G = typeof window !== 'undefined' ? window : globalThis;
  if (G.__vidnoteExtract) return;
  const isObj = v => v !== null && typeof v === 'object';
  const str = v => (v === undefined || v === null) ? '' : String(v);

  function mediaFrom(o) {
    let videos = [];
    if (Array.isArray(o.video_versions)) {
      videos = o.video_versions.filter(v => isObj(v) && typeof v.url === 'string')
        .map(v => ({ url: v.url, width: v.width || 0, height: v.height || 0 }));
    } else if (typeof o.video_url === 'string' && (o.shortcode || o.code)) {
      videos = [{ url: o.video_url, width: (o.dimensions || {}).width || 0, height: (o.dimensions || {}).height || 0 }];
    }
    if (!videos.length) return null;
    const user = isObj(o.user) ? o.user : (isObj(o.owner) ? o.owner : {});
    let caption = '';
    if (typeof o.caption === 'string') caption = o.caption;
    else if (isObj(o.caption) && typeof o.caption.text === 'string') caption = o.caption.text;
    else {
      const e = ((o.edge_media_to_caption || {}).edges || [])[0];
      if (e && isObj(e.node) && typeof e.node.text === 'string') caption = e.node.text;
    }
    const pk = str(o.pk || str(o.id).split('_')[0]);
    const code = str(o.code || o.shortcode);
    if (!pk && !code) return null;
    const num = (...xs) => { for (const x of xs) if (typeof x === 'number') return x; return null; };
    return {
      pk, code, videos, caption,
      uploader: str(user.username), uploader_name: str(user.full_name), owner_pk: str(user.pk || user.id),
      taken_at: num(o.taken_at, o.taken_at_timestamp),
      likes: num(o.like_count, (o.edge_media_preview_like || {}).count),
      comments: num(o.comment_count, (o.edge_media_to_parent_comment || {}).count, (o.edge_media_to_comment || {}).count),
      plays: num(o.play_count, o.ig_play_count, o.view_count, o.video_view_count, o.video_play_count),
    };
  }

  function commentFrom(o, parent) {
    const user = isObj(o.user) ? o.user : (isObj(o.owner) ? o.owner : null);
    const at = o.created_at || o.created_at_utc;
    if (typeof o.text !== 'string' || !o.text.trim() || !user || !(o.pk || o.id) || !at) return null;
    const likes = typeof o.comment_like_count === 'number' ? o.comment_like_count
      : (typeof (o.edge_liked_by || {}).count === 'number' ? o.edge_liked_by.count : 0);
    return {
      id: str(o.pk || o.id), media_pk: str(o.media_id), text: o.text, likes, at: Number(at) || 0,
      user: str(user.username), user_pk: str(user.pk || user.id),
      parent: str(o.parent_comment_id) || parent || '',
    };
  }

  // JSON ağacını dolaşır; bulduğu gönderi ve yorum kayıtlarını döndürür.
  function scan(root) {
    const out = { media: [], comments: [] };
    const stack = [[root, '', 0]];
    let seen = 0;
    while (stack.length && seen < 300000) {
      const [node, parent, depth] = stack.pop();
      seen++;
      if (!isObj(node) || depth > 60) continue;
      if (Array.isArray(node)) { for (let i = node.length - 1; i >= 0; i--) stack.push([node[i], parent, depth + 1]); continue; }
      let nextParent = parent;
      const m = mediaFrom(node);
      if (m) out.media.push(m);
      else {
        const c = commentFrom(node, parent);
        if (c) { out.comments.push(c); nextParent = c.id; }
      }
      for (const k in node) {
        if (k === 'caption') continue;              // açıklama kaydı yoruma benzer; yorum sayma
        const v = node[k];
        if (isObj(v)) stack.push([v, (k === 'user' || k === 'owner') ? parent : nextParent, depth + 1]);
      }
    }
    return out;
  }

  // Kısa kod -> sayısal gönderi kimliği (Instagram'ın 64'lük alfabesi)
  function codeToPk(code) {
    const abc = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
    let n = 0n;
    for (const ch of code.slice(0, 11)) { const i = abc.indexOf(ch); if (i < 0) return ''; n = n * 64n + BigInt(i); }
    return n.toString();
  }

  function codeFromPath(path) {
    const m = /\/(?:reel|reels|p|tv)\/([A-Za-z0-9_-]{5,})/.exec(path || '');
    return m ? m[1] : '';
  }

  G.__vidnoteExtract = { scan, codeToPk, codeFromPath };
})();
