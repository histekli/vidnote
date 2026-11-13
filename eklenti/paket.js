// Paket sayfası: videoyu indirir, tek .zip paketine koyar; VidNote'a gönderir ya da indirtir.
const $ = id => document.getElementById(id);
const DEFAULTS = { limit: 50, keepUsers: false, server: 'http://localhost:8765', autoSend: true };
let data = null, opts = null, zipBlob = null, zipName = 'vidnote_paket.zip', serverUp = false;

function status(text, isError, progress) {
  const s = $('status');
  s.className = 'status' + (isError ? ' err' : '');
  s.firstChild.textContent = text;
  $('bar').parentElement.hidden = progress === undefined;
  if (progress !== undefined) $('bar').style.width = Math.round(progress * 100) + '%';
}

function fact(label, value) {
  const span = document.createElement('span');
  const b = document.createElement('b'); b.textContent = value;
  span.append(label + ': ', b);
  $('facts').append(span);
}

function render() {
  $('title').textContent = data.uploader ? '@' + data.uploader : 'VidNote paketi';
  $('sub').textContent = data.url || '';
  const ul = $('diag');
  for (const d of data.diag || []) {
    const li = document.createElement('li');
    const mark = document.createElement('span'); mark.className = d.ok ? 'ok' : 'no'; mark.textContent = d.ok ? '✓ ' : '✗ ';
    li.append(mark, d.step + (d.detail ? ' — ' + d.detail : ''));
    ul.append(li);
  }
  if (!data.ok) return;
  $('factsCard').hidden = false;
  if (data.video.width) fact('Video', data.video.width + '×' + data.video.height);
  fact('Açıklama', data.caption ? data.caption.length + ' karakter' : 'yok');
  fact('Yorum', String(data.comments_fetched) + (data.stats && data.stats.comments != null ? ' / ' + data.stats.comments : ''));
  if (data.stats && data.stats.likes != null) fact('Beğeni', String(data.stats.likes));
  if (data.caption) { $('capCard').hidden = false; $('caption').textContent = data.caption; }
}

async function fetchVideo(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('Video indirilemedi (HTTP ' + r.status + ').');
  const total = Number(r.headers.get('content-length')) || 0;
  const reader = r.body.getReader(), chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value); got += value.length;
    status('Video indiriliyor… ' + (got / 1048576).toFixed(1) + ' MB', false, total ? got / total : 0.5);
  }
  const out = new Uint8Array(got);
  let p = 0;
  for (const c of chunks) { out.set(c, p); p += c.length; }
  return out;
}

function commentLines(list, indent) {
  let s = '';
  for (const c of list) {
    const tag = [c.by_owner ? 'video sahibi' : (c.user ? '@' + c.user : ''), c.likes ? c.likes + ' beğeni' : ''].filter(Boolean).join(', ');
    s += indent + '- ' + (tag ? '(' + tag + ') ' : '') + c.text.replace(/\s+/g, ' ') + '\n';
    if (c.replies) s += commentLines(c.replies, indent + '  ');
  }
  return s;
}

async function build() {
  const video = await fetchVideo(data.video.url);
  status('Paket hazırlanıyor…', false, 1);
  const enc = new TextEncoder();
  const pkg = {
    format: 'vidnote-paket/1', source: 'instagram', url: data.url, shortcode: data.shortcode,
    uploader: data.uploader, uploader_name: data.uploader_name, taken_at: data.taken_at, caption: data.caption,
    stats: data.stats, video_file: 'video.mp4', video: { width: data.video.width, height: data.video.height, bytes: video.length },
    comments: data.comments, comments_fetched: data.comments_fetched, captured_at: data.captured_at,
  };
  const files = [
    { name: 'video.mp4', data: video },
    { name: 'paket.json', data: enc.encode(JSON.stringify(pkg, null, 2)) },
    { name: 'aciklama.txt', data: enc.encode((data.uploader ? '@' + data.uploader + '\n' : '') + data.url + '\n\n' + (data.caption || '(açıklama yok)') + '\n') },
    { name: 'yorumlar.txt', data: enc.encode(data.comments.length ? commentLines(data.comments, '') : '(yorum alınmadı)\n') },
  ];
  zipBlob = new Blob(window.__vidnoteZip.makeZip(files), { type: 'application/zip' });
  zipName = 'instagram_' + (data.uploader || 'gonderi') + '_' + data.shortcode + '.zip';
  $('save').disabled = false;
  $('send').disabled = !serverUp;
}

async function ping() {
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 1500);
    const r = await fetch(opts.server.replace(/\/$/, '') + '/api/ping', { signal: ctl.signal });
    clearTimeout(t);
    serverUp = r.ok && (await r.json()).app === 'vidnote';
  } catch (e) { serverUp = false; }
  return serverUp;
}

async function send() {
  $('send').disabled = true;
  status('VidNote\'a gönderiliyor…', false);
  try {
    const fd = new FormData();
    fd.append('file', zipBlob, zipName);
    const r = await fetch(opts.server.replace(/\/$/, '') + '/api/jobs', { method: 'POST', body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
    status('VidNote\'a gönderildi. Döküm birkaç dakika içinde hazır olur.', false);
    $('open').href = opts.server; $('open').hidden = false;
  } catch (e) {
    status('Gönderilemedi: ' + e.message + ' Paketi indirip VidNote sayfasına sürükleyebilirsin.', true);
    $('send').disabled = false;
  }
}

function save() {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(zipBlob); a.download = zipName;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 60000);
}

async function main() {
  opts = Object.assign({}, DEFAULTS, await chrome.storage.local.get(Object.keys(DEFAULTS)));
  $('limit').value = String(opts.limit); $('keepUsers').checked = !!opts.keepUsers;
  $('autoSend').checked = !!opts.autoSend; $('server').value = opts.server;
  const savePrefs = () => chrome.storage.local.set({ limit: Number($('limit').value), keepUsers: $('keepUsers').checked, autoSend: $('autoSend').checked, server: $('server').value.trim() || DEFAULTS.server });
  for (const id of ['limit', 'keepUsers', 'autoSend', 'server']) $(id).addEventListener('change', savePrefs);
  $('send').addEventListener('click', send);
  $('save').addEventListener('click', save);
  $('copyDiag').addEventListener('click', async () => {
    const info = { url: data && data.url, ok: data && data.ok, error: data && data.error, video: !!(data && data.video), caption_chars: data && data.caption ? data.caption.length : 0,
                   comments: data && data.comments_fetched, diag: data && data.diag, server_up: serverUp, ua: navigator.userAgent };
    await navigator.clipboard.writeText(JSON.stringify(info, null, 2));
    $('copyDiag').textContent = 'Kopyalandı';
  });

  const id = new URLSearchParams(location.search).get('id');
  data = (await chrome.storage.session.get('paket_' + id))['paket_' + id];
  if (!data) { status('Paket bulunamadı. Instagram sekmesine dönüp eklenti düğmesine yeniden bas.', true); return; }
  render();
  if (!data.ok) { status(data.error || 'Veri alınamadı.', true); return; }
  await ping();
  try { await build(); } catch (e) { status(e.message, true); return; }
  const size = (zipBlob.size / 1048576).toFixed(1) + ' MB';
  if (serverUp && opts.autoSend) await send();
  else status(serverUp ? 'Paket hazır (' + size + ').' : 'Paket hazır (' + size + '). VidNote şu an çalışmıyor; paketi indirip sonra VidNote\'a verebilirsin.', false);
}
main();
