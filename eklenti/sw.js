// Arka plan: düğmeye basılınca açık sekmeden veriyi toplar ve paket sayfasını açar.
const DEFAULTS = { limit: 50, keepUsers: false, server: 'http://localhost:8765', autoSend: true };

async function capture(tab) {
  const id = Date.now().toString(36);
  let result;
  if (!/^https:\/\/www\.instagram\.com\//.test(tab.url || '')) {
    result = { ok: false, error: 'Bu düğme Instagram sayfalarında çalışır. Bir Reels ya da gönderi açıp yeniden dene.', url: tab.url || '', diag: [] };
  } else {
    try {
      await chrome.action.setBadgeText({ tabId: tab.id, text: '…' });
      const opts = Object.assign({}, DEFAULTS, await chrome.storage.local.get(Object.keys(DEFAULTS)));
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN', func: o => { window.__vidnoteOpts = o; }, args: [{ limit: Number(opts.limit), keepUsers: !!opts.keepUsers }] });
      const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN', files: ['extract.js', 'collect.js'] });
      result = (res && res.result) || { ok: false, error: 'Sayfadan veri alınamadı.', url: tab.url, diag: [] };
    } catch (e) {
      result = { ok: false, error: 'Sayfaya erişilemedi: ' + e.message, url: tab.url, diag: [] };
    } finally {
      chrome.action.setBadgeText({ tabId: tab.id, text: '' }).catch(() => {});
    }
  }
  await chrome.storage.session.set({ ['paket_' + id]: result });
  await chrome.tabs.create({ url: chrome.runtime.getURL('paket.html?id=' + id), index: (tab.index || 0) + 1 });
  return id;
}

chrome.action.onClicked.addListener(capture);
self.capture = capture;      // testlerin doğrudan çağırabilmesi için
