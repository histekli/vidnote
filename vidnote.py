#!/usr/bin/env python3
"""
vidnote — bir videoyu, yapay zekânın (ve senin) okuyabileceği bir bağlam paketine çevirir.

  video  ->  açıklama + zaman damgalı konuşma + ekrandaki yazılar (OCR) + anahtar kareler
         ->  transkript.md (derli toplu döküm), sheets/ (kareler), pack.md / pack.json (ayrıntı)

Kullanım:
  python3 vidnote.py video.mp4
  python3 vidnote.py "https://www.youtube.com/watch?v=..." --lang tr
  python3 vidnote.py video.mp4 --max-frames 40 --whisper-model small

Gerekenler: ffmpeg, numpy, Pillow. İsteğe bağlı: tesseract (ekran yazısı),
faster-whisper veya sherpa-onnx (konuşma), yt-dlp (URL).
"""
import argparse
import datetime
import difflib
import http.cookiejar
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import wave
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

# ---- kare seçimi ayarları -------------------------------------------------
AW = AH = 192         # analiz karesi boyutu (piksel)
GRID = 24             # 24x24 blok (8x8 piksel)
BLOCK_THR = 14.0      # bir bloğun "değişti" sayılması için ortalama fark (0-255)
LOCAL_THR = 0.008     # blokların >=%0,8'i değiştiyse: küçük/yerel değişim (yazı, simge)
GLOBAL_THR = 0.12     # >=%12: belirgin değişim
CUT_THR = 0.60        # ardışık iki kare arasında >=%60: sahne kesmesi
SETTLE_THR = 0.006    # ardışık kare farkı bunun altındaysa görüntü "durulmuş"
HOLD = {"scene": 0.25, "change": 0.25, "local": 0.5}  # durulma süresi (sn)
MAX_WAIT = 1.5        # durulmayan (sürekli hareketli) görüntüde bekleme sınırı (sn)
WINDOW = 40           # A-B-A dönüşlerini yakalamak için bakılan son kare sayısı
SCENE_WAIT = 0.5      # kesmeden sonra görüntü durulmasa da en geç bu kadar bekle (sn)
STILL_STEP = 0.004    # durulmuş sayılmak için ardışık karede kıpırdayan blok oranı üst sınırı
SHOT_THR = 0.30       # hareketli çekimde "aynı plan" eşiği (3x3 renk dağılımı farkı)
RECHECK = 0.5         # hareketli çekimde yeniden bakma aralığı (sn)
PRIORITY = {"start": 10.0, "scene": 3.0, "text": 2.0, "change": 1.0, "local": 0.5}
LABEL = {"start": "açılış", "scene": "yeni sahne", "change": "görüntü değişti", "local": "ekrana yeni öğe geldi",
         "text": "ekrana yeni yazı geldi"}
FRAME_SCALE = "scale='if(gt(iw,ih),min(iw,{0}),-2)':'if(gt(iw,ih),-2,min(ih,{0}))'"


class VidnoteError(Exception):
    """Kullanıcıya gösterilecek, beklenen hata (dosya yok, indirilemedi...)."""


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def ff(cmd):
    """ffmpeg/ffprobe çalıştırır; hata olursa aracın kendi mesajını okunur biçimde verir."""
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        tail = " | ".join((r.stderr or "").strip().splitlines()[-3:])
        raise VidnoteError(f"{Path(cmd[0]).name} hata verdi (kod {r.returncode}): {tail or 'ayrıntı yok'}")
    return r.stdout


def mmss(t):
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


# ---- girdi ----------------------------------------------------------------
def parse_json3(data, max_sec=8.0, max_words=28):
    """YouTube'un json3 altyazısını zaman damgalı, okunur uzunlukta bölümlere çevirir."""
    out = []
    for ev in data.get("events") or []:
        text = " ".join("".join(x.get("utf8", "") for x in ev.get("segs") or []).split())
        if not text:
            continue
        start = (ev.get("tStartMs") or 0) / 1000
        end = start + (ev.get("dDurationMs") or 0) / 1000
        last = out[-1] if out else None
        if last and start - last["start"] < max_sec and len(last["text"].split()) < max_words and not last["text"].endswith((".", "?", "!")):
            last["text"] += " " + text                   # otomatik altyazı 2-3 kelimelik parçalar hâlinde gelir
            last["end"] = max(last["end"], end)
        else:
            out.append({"start": start, "end": end, "text": text})
    return out


def site_captions(ydl, info, want_lang):
    """Sitenin (YouTube) hazır altyazısını bulur: önce elle yazılmış, sonra otomatik.
    (bölümler, dil, ad) döndürür; yoksa ([], None, None)."""
    lang = (want_lang or info.get("language") or "").split("-")[0]
    cands = []
    for table, label in ((info.get("subtitles") or {}, "sitenin altyazısı"), (info.get("automatic_captions") or {}, "sitenin otomatik altyazısı")):
        keys = [k for k in table if lang and (k == lang or k.startswith(lang + "-"))]
        keys.sort(key=lambda k: (not k.endswith("-orig"), k != lang, k))
        for k in keys[:2]:
            fmt = next((f for f in table[k] if f.get("ext") == "json3" and f.get("url")), None)
            if fmt:
                cands.append((fmt["url"], k, label))
    for cap_url, key, label in cands[:3]:
        try:
            segs = parse_json3(json.loads(ydl.urlopen(cap_url).read().decode("utf-8")))
        except Exception as e:
            log(f"uyarı: altyazı alınamadı ({key}): {str(e)[:120]}")
            continue
        if len(segs) >= 2:
            return segs, key.split("-")[0], f"{label} ({key})"
    return [], None, None


def fetch_url(url, workdir, cookies_from_browser=None, cookies_file=None, max_comments=0, progress=None,
              max_height=720, use_captions=True, lang=None):
    """URL'yi yt-dlp ile indirir (en fazla 720p). (yol, üst veri) döndürür.
    Üst veri: başlık, açıklama, hesap, yayın tarihi, sayfa adresi."""
    try:
        import yt_dlp
    except ImportError:
        raise VidnoteError("Link için yt-dlp gerekli:  pip install yt-dlp")
    opts = {
        "outtmpl": str(Path(workdir) / "source.%(ext)s"),
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    def hook(d):                               # indirme sürerken yüzdeyi arayüze bildir
        if not progress or d.get("status") != "downloading":
            return
        done = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        what = "Ses" if (d.get("info_dict") or {}).get("vcodec") == "none" else "Video"
        if total:
            progress(f"{what} indiriliyor: %{int(done * 100 / total)} ({done / 1048576:.1f} / {total / 1048576:.1f} MB)", min(done / total, 1.0))
        else:
            progress(f"{what} indiriliyor: {done / 1048576:.1f} MB", None)
    opts["progress_hooks"] = [hook]
    if max_comments > 0:                       # yorumlar isteğe bağlı; bazı sitelerde yavaştır
        opts["getcomments"] = True
        opts["extractor_args"] = {"youtube": {"max_comments": [str(max_comments), "all", "20", "5"]}}
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if cookies_file:
        if not Path(cookies_file).expanduser().exists():
            raise VidnoteError(f"Çerez dosyası bulunamadı: {cookies_file}")
        opts["cookiefile"] = str(Path(cookies_file).expanduser())
    ready = None
    try:
        if use_captions and re.search(r"//([\w.]*\.)?(youtube\.com|youtu\.be)/", url):
            # Hazır altyazı varsa ses indirilmez ve konuşma çözülmez: hem veri hem süre kazanılır
            try:
                with yt_dlp.YoutubeDL(dict(opts, progress_hooks=[])) as ydl:
                    pre = ydl.extract_info(url, download=False)
                    segs, cap_lang, cap_name = site_captions(ydl, pre, lang)
                if segs:
                    ready = (segs, cap_lang, cap_name)
                    opts["format"] = f"bv*[height<={max_height}]/b[height<={max_height}]/b"
                    log(f"hazır altyazı bulundu: {cap_name}; yalnızca görüntü indirilecek")
            except Exception as e:
                log(f"uyarı: hazır altyazıya bakılamadı ({str(e)[:120]}); video sesiyle indirilecek")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
    except Exception as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        raise VidnoteError(f"Video indirilemedi: {msg}\nGiriş isteyen sitelerde (ör. Instagram) ayarlara çerez dosyası (COOKIES_FILE) "
                           "ekleyin ya da videoyu indirip dosya olarak verin.")
    p = Path(path)
    if not p.exists():
        cands = list(Path(workdir).glob("source.*"))
        if not cands:
            raise VidnoteError("Video indirildi ama dosya bulunamadı.")
        p = cands[0]
    date = info.get("upload_date") or ""
    meta = {
        "title": info.get("title") or url,
        "description": info.get("description") or "",
        "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
        "upload_date": f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else "",
        "webpage_url": info.get("webpage_url") or url,
        "site": info.get("extractor_key") or "",
        "stats": {k: v for k, v in (("likes", info.get("like_count")), ("comments", info.get("comment_count")),
                                    ("plays", info.get("view_count"))) if isinstance(v, (int, float))},
    }
    if ready:
        meta["ready_transcript"] = ready
    return str(p), meta, comments_from_ytdlp(info, max_comments) if max_comments > 0 else []


VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def collect_gallery(folder):
    """gallery-dl'nin indirdiği klasörden görselleri, videoları ve gönderi bilgisini toplar.
    Dosya adları sıra numarasıyla başladığı için ad sırası = gönderideki sıra."""
    files = sorted(f for f in Path(folder).iterdir() if f.suffix.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES)
    info = {}
    for f in files:
        j = Path(str(f) + ".json")
        if j.exists():
            try:
                d = json.loads(j.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if not info or (d.get("description") and not info.get("description")):
                info = d
    user, code = info.get("username") or "", info.get("post_shortcode") or ""
    meta = {
        "title": " · ".join(x for x in (f"@{user}" if user else "", code) if x) or "Instagram gönderisi",
        "description": info.get("description") or "",
        "uploader": user,
        "upload_date": str(info.get("post_date") or info.get("date") or "")[:10],
        "webpage_url": info.get("post_url") or "",
        "site": "Instagram",
        "stats": {"likes": info["likes"]} if isinstance(info.get("likes"), (int, float)) and info["likes"] > 0 else {},
    }
    return ([f for f in files if f.suffix.lower() in IMAGE_SUFFIXES],
            [f for f in files if f.suffix.lower() in VIDEO_SUFFIXES], meta)


def fetch_instagram_post(url, workdir, cookies_from_browser=None, cookies_file=None):
    """Görsel ya da çoklu Instagram gönderisini gallery-dl ile indirir (yt-dlp yalnızca video indirir)."""
    dest = Path(workdir) / "gal"
    dest.mkdir(exist_ok=True)
    cmd = [sys.executable, "-m", "gallery_dl", "-q", "-D", str(dest), "--write-metadata", "--no-mtime",
           "-f", "{num:>03}_{media_id}.{extension}"]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    if cookies_file:
        cmd += ["--cookies", str(Path(cookies_file).expanduser())]
    try:
        r = subprocess.run(cmd + [url], capture_output=True, text=True, errors="replace", timeout=900)
    except subprocess.TimeoutExpired:
        raise VidnoteError("Gönderi indirilemedi: işlem 15 dakikada bitmedi.")
    images, videos, meta = collect_gallery(dest)
    if not images and not videos:
        err = (r.stderr or "").strip()
        if "No module named" in err:
            raise VidnoteError("Görsel gönderiler için gallery-dl gerekli. Kurulum klasöründe ./kur.sh komutunu yeniden çalıştır.")
        if "login" in err.lower():
            if cookies_from_browser or cookies_file:
                raise VidnoteError("Instagram bu gönderi için giriş istiyor ve ayarlardaki çerezlerle giriş kabul edilmedi. "
                                   "O tarayıcıda Instagram'a giriş yapılı mı? Çıkış yapıp yeniden girmek çoğu zaman çözer.")
            raise VidnoteError("Instagram bu gönderi için giriş istiyor. VidNote klasöründeki ayarlar.env dosyasında "
                               "COOKIES_BROWSER= satırına Instagram'a giriş yaptığın tarayıcıyı yaz (chrome, safari, firefox, brave, edge), "
                               "sonra VidNote'u kapatıp yeniden aç.")
        raise VidnoteError("Gönderi indirilemedi: " + (" | ".join(err.splitlines()[-2:]) or "gallery-dl hiçbir dosya indirmedi"))
    if not meta["webpage_url"]:
        meta["webpage_url"] = url
    return images, videos, meta


# ---- Threads (threads.com / threads.net) ----------------------------------
THREADS_RE = re.compile(r"//(www\.)?threads\.(com|net)/")
THREADS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")
THREADS_MAX_ITEMS = 20          # carousel'de en fazla bu kadar öğe indirilir (aşırı indirmeye karşı)
THREADS_LOGIN_HELP = (
    "Threads bu gönderinin verisini vermedi (büyük olasılıkla giriş duvarı). Threads gönderi verisini yalnızca "
    "giriş yapmış oturuma gömüyor; bu yüzden çerez gerekli. VidNote klasöründeki ayarlar.env dosyasında "
    "COOKIES_BROWSER= satırına Threads'e (threads.com) giriş yaptığın tarayıcıyı yaz (chrome, firefox, brave, edge), "
    "ya da COOKIES_FILE= satırına o tarayıcıdan dışa aktardığın cookies.txt dosyasının yolunu yaz; sonra VidNote'u "
    "kapatıp yeniden aç.")


def _threads_shortcode(url):
    """Threads linkinden gönderi kodunu çıkarır (sorgu parametreleri yok sayılır).
    /share/ linkleri yönlendirmeli olduğundan son adresten çözülmelidir."""
    m = re.search(r"/(?:post|t)/([A-Za-z0-9_-]+)", url.split("?")[0])
    return m.group(1) if m else None


def _sjs_blocks(html):
    """Sayfadaki data-sjs JSON <script> bloklarının içeriğini döndürür."""
    blocks = []
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.DOTALL):
        attrs, body = m.group(1), m.group(2)
        if "application/json" in attrs and "data-sjs" in attrs:
            blocks.append(body)
    return blocks


def _find_thread_items(obj):
    """JSON ağacında (iç içe) 'thread_items' listelerini toplar."""
    found = []
    if isinstance(obj, dict):
        if isinstance(obj.get("thread_items"), list):
            found.append(obj["thread_items"])
        for v in obj.values():
            found += _find_thread_items(v)
    elif isinstance(obj, list):
        for v in obj:
            found += _find_thread_items(v)
    return found


def _threads_json_from_html(html, code):
    """Threads sayfasının HTML'inden, kodu verilen gönderinin post nesnesini çıkarır (ağ kullanmaz).
    Veri yoksa (giriş duvarı) ya da kod eşleşmezse anlaşılır VidnoteError verir."""
    posts = []
    for block in _sjs_blocks(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for items in _find_thread_items(data):
            for it in items:
                post = it.get("post") if isinstance(it, dict) else None
                if not isinstance(post, dict) and isinstance(it, dict) and it.get("code"):
                    post = it                       # bazı yapılarda öğe doğrudan gönderidir
                if isinstance(post, dict):
                    posts.append(post)
    if not posts:
        raise VidnoteError(THREADS_LOGIN_HELP)
    match = next((p for p in posts if p.get("code") == code), None)
    if match is None:                               # sayfada başka gönderiler (yanıt, öneri) olabilir; onları işleme
        raise VidnoteError(f"Threads sayfasında {code} kodlu gönderi bulunamadı; sayfadaki başka bir gönderi "
                           "işlenmedi. Linkin işlemek istediğin gönderiye ait olduğundan emin ol.")
    return match


def _threads_media(post):
    """Gönderideki tüm medya öğelerini sırayla döndürür; carousel (iç içe olabilir) açılır."""
    if isinstance(post.get("carousel_media"), list):
        out = []
        for child in post["carousel_media"]:
            if isinstance(child, dict):
                out += _threads_media(child)
        return out[:THREADS_MAX_ITEMS]
    return [post]


def _threads_best_image(media):
    """Medyadaki en geniş görsel URL'si (.jpg) ya da None."""
    cands = [c for c in ((media.get("image_versions2") or {}).get("candidates") or []) if c.get("url")]
    return max(cands, key=lambda c: c.get("width") or 0)["url"] if cands else None


def _threads_best_video(media):
    """Medyadaki en geniş video URL'si (.mp4) ya da None."""
    vers = [v for v in (media.get("video_versions") or []) if v.get("url")]
    return max(vers, key=lambda v: v.get("width") or 0)["url"] if vers else None


def _threads_meta(post, url):
    """post nesnesinden üst veriyi üretir (ağ kullanmaz)."""
    user = (post.get("user") or {}).get("username") or ""
    code = post.get("code") or ""
    taken = post.get("taken_at")
    likes = post.get("like_count")
    date = (datetime.datetime.fromtimestamp(int(taken), datetime.timezone.utc).strftime("%Y-%m-%d")
            if isinstance(taken, (int, float)) and taken > 0 else "")
    canonical = f"https://www.threads.com/@{user}/post/{code}" if user and code else url.split("?")[0]
    return {
        "title": f"@{user} · {code}" if user else "Threads gönderisi",
        "description": (post.get("caption") or {}).get("text") or "",
        "uploader": user,
        "upload_date": date,
        "webpage_url": canonical,
        "site": "Threads",
        "stats": {"likes": likes} if isinstance(likes, (int, float)) and likes > 0 else {},
    }


def _threads_cookies(cookies_from_browser, cookies_file):
    """Threads istekleri için çerez kavanozu üretir. cookies_file: Netscape (cookies.txt) biçimi.
    cookies_from_browser: yt-dlp'nin çerez okuyucusuyla tarayıcıdan alınmaya çalışılır; olmazsa None döner."""
    if cookies_file:
        path = Path(cookies_file).expanduser()
        if not path.exists():
            raise VidnoteError(f"Çerez dosyası bulunamadı: {cookies_file}")
        jar = http.cookiejar.MozillaCookieJar(str(path))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError) as e:
            raise VidnoteError(f"Çerez dosyası okunamadı ({cookies_file}): {str(e)[:120]}")
        return jar
    if cookies_from_browser:
        try:                        # yt-dlp'nin iç API'si sürüme göre değişebilir: bağlanma, dene, olmazsa geç
            from yt_dlp.cookies import extract_cookies_from_browser
            return extract_cookies_from_browser(cookies_from_browser)
        except Exception as e:
            log(f"uyarı: tarayıcıdan ({cookies_from_browser}) çerez okunamadı: {type(e).__name__}: {str(e)[:120]}")
    return None


def _threads_fetch(url, dest, referer):
    """Tek bir medya dosyasını akış halinde indirir; olmazsa uyarı verip False döner."""
    try:
        with requests.get(url, headers={"User-Agent": THREADS_UA, "Referer": referer},
                          stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if chunk:
                        f.write(chunk)
        return True
    except requests.RequestException as e:
        log(f"uyarı: Threads medyası indirilemedi ({Path(dest).name}): {str(e)[:120]}")
        return False


def _threads_download_media(post, referer, workdir):
    """Gönderideki görselleri ve videoları workdir altına sıralı adlarla indirir."""
    images, videos = [], []
    for item in _threads_media(post):
        if len(images) + len(videos) >= THREADS_MAX_ITEMS:
            break
        vurl = _threads_best_video(item)
        if vurl:                                    # video öğesinde önizleme görseli yerine videoyu al
            dest = Path(workdir) / f"thr_vid_{len(videos) + 1:03d}.mp4"
            if _threads_fetch(vurl, dest, referer):
                videos.append(dest)
            continue
        iurl = _threads_best_image(item)
        if iurl:
            dest = Path(workdir) / f"thr_{len(images) + 1:03d}.jpg"
            if _threads_fetch(iurl, dest, referer):
                images.append(dest)
    return images, videos


def fetch_threads_post(url, workdir, cookies_from_browser=None, cookies_file=None):
    """Threads (threads.com / threads.net) gönderisini çeker ve medyasını indirir.
    (görseller, videolar, üst veri) döndürür — fetch_instagram_post ile aynı biçim.
    Threads gönderi verisini yalnızca girişli oturuma gömdüğü için çerez gerekir; yalnız metin
    gönderisinde ([], [], meta) döner. Veri yoksa anlaşılır bir VidnoteError verilir."""
    jar = _threads_cookies(cookies_from_browser, cookies_file)
    try:
        r = requests.get(url, headers={"User-Agent": THREADS_UA, "Accept-Language": "tr,en;q=0.8"},
                         cookies=jar, allow_redirects=True, timeout=60)
    except requests.RequestException as e:
        raise VidnoteError(f"Threads gönderisine ulaşılamadı: {str(e)[:150]}")
    code = _threads_shortcode(str(r.url)) or _threads_shortcode(url)
    if not code:
        raise VidnoteError("Threads linkinden gönderi kodu çıkarılamadı. Bağlantı /@kullanici/post/<kod>, "
                           "/t/<kod> ya da /share/<kod> biçiminde olmalı.")
    post = _threads_json_from_html(r.text, code)
    meta = _threads_meta(post, str(r.url))
    images, videos = _threads_download_media(post, meta["webpage_url"], workdir)
    return images, videos, meta


def open_package(zip_path, workdir):
    """VidNote paketini açar: içinde bir video ve/veya görseller ile paket.json (açıklama, hesap, yorumlar) bulunur.
    Tarayıcı eklentisi bu paketi üretir. (video yolu ya da None, görseller, üst veri, yorumlar) döndürür."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise VidnoteError("Bu .zip dosyası açılamadı.")
    with zf:
        names = sorted((i for i in zf.infolist() if not i.is_dir()), key=lambda i: i.filename)
        vid = next((i for i in names if Path(i.filename).suffix.lower() in VIDEO_SUFFIXES), None)
        pics = [i for i in names if Path(i.filename).suffix.lower() in IMAGE_SUFFIXES][:40]
        if vid is None and not pics:
            raise VidnoteError("Pakette video ya da görsel yok.")
        if any(i.file_size > 4 * 1024 ** 3 for i in ([vid] if vid else []) + pics):
            raise VidnoteError("Paketteki dosya çok büyük.")
        dest, images = None, []
        if vid is not None:
            dest = Path(workdir) / ("source" + Path(vid.filename).suffix.lower())    # dosya adını pakete bırakma
            with zf.open(vid) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
        for n, i in enumerate(pics, 1):
            f = Path(workdir) / f"pkgimg_{n:03d}{Path(i.filename).suffix.lower()}"
            with zf.open(i) as src, open(f, "wb") as out:
                shutil.copyfileobj(src, out)
            images.append(f)
        info = {}
        js = next((i for i in names if Path(i.filename).name == "paket.json"), None)
        if js is not None and js.file_size < 20 * 1024 ** 2:
            try:
                info = json.loads(zf.read(js).decode("utf-8"))
            except ValueError:
                log("uyarı: paket.json okunamadı; yalnızca video işlenecek")
    stats = info.get("stats") or {}
    meta = {
        "title": (f"@{info['uploader']}" if info.get("uploader") else "") + (" · " + info["shortcode"] if info.get("shortcode") else "")
                 or Path(zip_path).stem,
        "description": info.get("caption") or "",
        "uploader": info.get("uploader") or "",
        "upload_date": (info.get("taken_at") or "")[:10],
        "webpage_url": info.get("url") or "",
        "site": info.get("source") or "",
        "stats": {k: stats[k] for k in ("likes", "comments", "plays") if isinstance(stats.get(k), (int, float))},
    }
    comments = [c for c in (info.get("comments") or []) if isinstance(c, dict) and str(c.get("text", "")).strip()]
    return (str(dest) if dest else None), images, meta, comments


def comments_from_ytdlp(info, limit):
    """yt-dlp'nin getirdiği yorumları paket biçimine çevirir (yanıtlar üst yorumun altına)."""
    top, by_id = [], {}
    for c in (info.get("comments") or [])[: limit * 3]:
        item = {"text": str(c.get("text") or "").strip(), "likes": c.get("like_count") or 0,
                "by_owner": bool(c.get("author_is_uploader")), "replies": []}
        if not item["text"]:
            continue
        by_id[c.get("id")] = item
        parent = by_id.get(c.get("parent"))
        (parent["replies"] if parent else top).append(item)
    return top[:limit]


def probe(path):
    out = ff(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    info = json.loads(out)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if v is None:
        raise VidnoteError("Dosyada video akışı yok.")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []):
        rot = int(sd.get("rotation", rot) or rot)
    if abs(rot) % 180 == 90:
        w, h = h, w
    dur = float(info["format"].get("duration") or v.get("duration") or 0)
    return {"duration": dur, "width": w, "height": h,
            "has_audio": any(s["codec_type"] == "audio" for s in info["streams"])}


# ---- 1) kare seçimi -------------------------------------------------------
def changed_fraction(a, b):
    """İki analiz karesi arasında değişen blokların oranı (0..1)."""
    d = np.abs(a - b).mean(axis=2)
    blocks = d.reshape(GRID, AH // GRID, GRID, AW // GRID).mean(axis=(1, 3))
    return float((blocks > BLOCK_THR).mean())


def grid_hist(f, g=3, bins=6):
    """Kareyi 3x3 bölgeye ayırıp her bölgenin renk dağılımını çıkarır. Kamera ya da
    kişi kıpırdadığında az değişir; ekrana yeni bir görsel girdiğinde çok değişir."""
    q = (f.astype(np.int32) * bins) // 256
    idx = q[..., 0] * bins * bins + q[..., 1] * bins + q[..., 2]
    s = AH // g
    return np.stack([np.bincount(idx[i * s:(i + 1) * s, j * s:(j + 1) * s].ravel(), minlength=bins ** 3)
                     for i in range(g) for j in range(g)]) / float(s * s)


def shot_distance(h1, h2):
    """9 bölgeden en çok değişen 3.'sünün farkı: karenin kabaca üçte biri yenilendi mi?"""
    return float(np.sort(np.abs(h1 - h2).sum(axis=1) / 2)[-3])


def select_frames(path, fps, duration, max_frames):
    """Videoyu tek geçişte küçük karelerle tarar; anlamlı değişimlerde,
    görüntü durulduktan sonra kare seçer. Tekrar eden çekimleri atlar."""
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps={fps},scale={AW}:{AH}:flags=area",
           "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    nbytes = AW * AH * 3
    kept, revisits, motions = [], [], []
    cur = 0               # ekranda şu an hangi seçilmiş karenin planı var
    since_new = False     # son dönüş notundan beri yeni bir kare seçildi mi
    next_ok_t = 0.0       # hareketli çekimde bir sonraki kontrol zamanı
    ref = prev = None
    pending = None
    stable_run = 0
    i = -1
    while True:
        buf = proc.stdout.read(nbytes)
        if len(buf) < nbytes:
            break
        i += 1
        f = np.frombuffer(buf, np.uint8).reshape(AH, AW, 3).astype(np.int16)
        t = i / fps
        if prev is None:
            motion, step = 0.0, 0.0
        else:
            motion = float(np.abs(f - prev).mean() / 255.0)
            step = changed_fraction(f, prev)
        stable_run = stable_run + 1 if (motion <= SETTLE_THR and step <= STILL_STEP) else 0
        motions.append(motion)
        prev = f

        if ref is None:                       # ilk kare
            kept.append({"idx": i, "t": t, "t0": t, "reason": "start", "score": 1.0, "thumb": f, "hist": grid_hist(f)})
            ref = f
            continue

        frac = changed_fraction(f, ref)
        if frac < LOCAL_THR:                  # ekrandaki durum değişmedi (ya da eski haline döndü)
            pending = None
            continue
        if pending is None:
            pending = {"first": i, "since": i, "score": frac, "cut_at": None}
        pending["score"] = max(pending["score"], frac)
        if step >= CUT_THR and pending["cut_at"] is None:
            pending["cut_at"] = i
        is_cut = pending["cut_at"] is not None
        reason = "scene" if is_cut else ("change" if pending["score"] >= GLOBAL_THR else "local")

        settled = stable_run / fps >= HOLD[reason]
        if is_cut:      # yeni sahne: hareketli olsa bile ilk karesini geciktirme
            forced = (i - pending["cut_at"]) / fps >= SCENE_WAIT and t >= next_ok_t
        else:           # sürekli hareket (elde kamera, konuşan kişi): aralıklarla bak
            forced = (i - pending["first"]) / fps >= MAX_WAIT and t >= next_ok_t
        if not (settled or forced):
            continue
        # t0: bu görüntünün ekranda başladığı an (kare biraz sonra, durulunca alınır)
        t0 = pending["cut_at"] / fps if is_cut else (pending["since"] / fps if settled else t)
        window = list(reversed(range(max(0, len(kept) - WINDOW), len(kept))))

        if settled:
            # Durağan içerik (slayt, ekran kaydı, grafik): blok bazında birebir karşılaştır
            dup = next((k for k in window if changed_fraction(f, kept[k]["thumb"]) < LOCAL_THR), None)
            h = None
        else:
            # Hareketli çekim: kıpırtıyı değil, kadraja yeni bir şey girip girmediğini ölç
            h = grid_hist(f)
            if shot_distance(h, kept[cur]["hist"]) < SHOT_THR:     # aynı plan sürüyor
                next_ok_t = t + RECHECK
                pending.update(since=i, score=frac, cut_at=None)
                continue
            dup = next((k for k in window if shot_distance(h, kept[k]["hist"]) < SHOT_THR), None)
        if dup is not None:                                   # daha önce gösterilen plana dönüş (A-B-A)
            if dup != cur and since_new:
                revisits.append({"t": t0, "at": t, "of": dup})
                since_new = False
            cur = dup
        else:
            kept.append({"idx": i, "t": t, "t0": t0, "reason": reason, "score": pending["score"],
                         "settled": bool(settled), "thumb": f, "hist": h if h is not None else grid_hist(f)})
            cur = len(kept) - 1
            since_new = True
        next_ok_t = t + RECHECK
        ref = f
        pending = None
    proc.wait()
    analyzed = i + 1

    # Bütçe: fazla kare varsa en az bilgi taşıyanları at
    while len(kept) > max_frames:
        j = min(range(1, len(kept)),
                key=lambda k: PRIORITY[kept[k]["reason"]] + kept[k]["score"] - (0 if kept[k].get("settled", True) else 0.5))
        for r in revisits:                   # dönüş kayıtlarının indekslerini düzelt
            if r["of"] == j:
                r["of"] = None
            elif r["of"] is not None and r["of"] > j:
                r["of"] -= 1
        kept.pop(j)
    revisits = [r for r in revisits if r["of"] is not None]
    return kept, revisits, analyzed, motions


def extract_frames(path, fps, kept, outdir, full_side=1280):
    """Seçilen kareleri (OCR için) yüksek çözünürlükte dışarı alır."""
    outdir.mkdir(parents=True, exist_ok=True)
    sel = "+".join(f"eq(n\\,{k['idx']})" for k in kept)
    scale = f"scale='if(gt(iw,ih),min(iw,{full_side}),-2)':'if(gt(iw,ih),-2,min(ih,{full_side}))'"
    head = ["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", f"fps={fps},select='{sel}',{scale}"]
    tail = ["-q:v", "2", str(outdir / "raw_%04d.jpg")]
    try:                                            # yeni ffmpeg: -vsync kaldırıldı, yerine -fps_mode geldi
        ff(head + ["-fps_mode", "passthrough"] + tail)
    except VidnoteError as first:
        try:                                        # ffmpeg 5.1 öncesi yalnızca -vsync'i tanır
            ff(head + ["-vsync", "0"] + tail)
        except VidnoteError:
            raise first
    files = sorted(outdir.glob("raw_*.jpg"))
    if len(files) != len(kept):
        log(f"uyarı: {len(kept)} kare beklenirken {len(files)} çıktı")
    return files


# ---- 2) ekran yazısı (OCR) ------------------------------------------------
def ocr_image(path, langs):
    """Tesseract ile karedeki yazı satırlarını (yukarıdan aşağıya) döndürür."""
    out = subprocess.run(["tesseract", str(path), "stdout", "-l", langs, "--psm", "11", "tsv"],
                         capture_output=True, text=True).stdout
    lines = {}
    for row in out.splitlines()[1:]:
        r = row.split("\t")
        if len(r) < 12:
            continue
        try:
            conf = float(r[10])
        except ValueError:
            continue
        word = r[11].strip()
        if conf < 55 or not re.search(r"\w", word):
            continue
        key = (int(r[2]), int(r[3]), int(r[4]))
        ln = lines.setdefault(key, {"top": int(r[7]), "words": []})
        ln["words"].append((int(r[6]), word))
    result = []
    for ln in sorted(lines.values(), key=lambda v: v["top"]):
        text = " ".join(w for _, w in sorted(ln["words"]))
        if len(re.findall(r"\w", text)) >= 3:
            result.append(text)
    return result


_tess_cache = {}
_tess_min = {}        # okunan satır -> en düşük kelime güveni (konuşmayı düzeltirken hangi satıra güvenileceği)
MIN_TEXT_H = 0.012    # kare yüksekliğinin %1,2'sinden küçük satırlar OCR'a alınmaz


def tess_line(crop, langs):
    """Tek satırlık kırpıntıyı tesseract ile okur -> (metin, ortalama güven)."""
    import cv2
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if g.shape[0] < 72:                                   # küçük yazıyı büyüt
        k = 72 / g.shape[0]
        g = cv2.resize(g, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
    if g.mean() < 110:                                    # koyu zemin üstü açık yazı -> ters çevir
        g = 255 - g
    g = cv2.copyMakeBorder(g, 12, 12, 12, 12, cv2.BORDER_REPLICATE)
    ok, png = cv2.imencode(".png", g)
    out = subprocess.run(["tesseract", "stdin", "stdout", "-l", langs, "--psm", "7", "tsv"],
                         input=png.tobytes(), capture_output=True).stdout.decode("utf-8", "replace")
    words, confs = [], []
    for row in out.splitlines()[1:]:
        r = row.split("\t")
        if len(r) >= 12 and re.search(r"\w", r[11]):
            words.append(r[11].strip())
            confs.append(float(r[10]))
    _tess_min[" ".join(words)] = min(confs) if confs else 0.0
    return " ".join(words), (sum(confs) / len(confs) if confs else 0.0)


def ocr_frame(path, langs, detector, have_tess):
    """Önce metin kutularını bulur (karmaşık arka planda da çalışır), sonra her
    satırı Türkçe destekli tesseract ile okur. Tesseract zayıf kalırsa
    dedektörün kendi okumasına (Türkçe karakter bilmez) düşer."""
    if detector is None:
        return [{"text": x, "y": None, "h": None} for x in ocr_image(path, langs)] if have_tess else []
    import cv2
    img = cv2.imread(str(path))
    res, _ = detector(img, use_cls=False)           # yazı yönü sınıflandırması gereksiz; atlamak hızlandırır
    H, W = img.shape[:2]
    # kutuları dikdörtgene çevir; aynı satırdaki komşu kutuları birleştir
    rects = []
    for box, rtxt, rconf in res or []:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        rects.append([min(xs), min(ys), max(xs), max(ys), rtxt.strip(), float(rconf)])
    rects.sort(key=lambda r: (r[1], r[0]))
    merged = []
    for r in rects:
        m = merged[-1] if merged else None
        if m:
            h = min(m[3] - m[1], r[3] - r[1])
            overlap = min(m[3], r[3]) - max(m[1], r[1])
            a, b = (m, r) if m[0] <= r[0] else (r, m)
            if overlap >= 0.5 * h and b[0] - a[2] <= 1.2 * h:
                merged[-1] = [a[0], min(m[1], r[1]), max(m[2], r[2]), max(m[3], r[3]), a[4] + " " + b[4], min(m[5], r[5])]
                continue
        merged.append(r)
    lines = []
    for bx0, by0, bx1, by1, text, rconf in merged:
        h = by1 - by0
        x0, x1 = int(max(0, bx0 - h * 0.3)), int(min(W, bx1 + h * 0.3))
        y0, y1 = int(max(0, by0 - h * 0.2)), int(min(H, by1 + h * 0.2))
        conf = 0.0                      # yalnızca tesseract okuması seçilirse dolar
        if have_tess and y1 - y0 >= 8:
            key = (text, x0 // 16, y0 // 16, x1 // 16, y1 // 16)
            if key not in _tess_cache:
                _tess_cache[key] = tess_line(img[y0:y1, x0:x1], langs)
            ttxt, tconf = _tess_cache[key]
            n_r, n_t = len(re.findall(r"\w", text)), len(re.findall(r"\w", ttxt))
            if tconf >= 60 and n_t >= 0.7 * n_r:
                text, conf = ttxt, _tess_min.get(ttxt, 0.0)
        if h / H < MIN_TEXT_H:          # okunamayacak kadar küçük yazı: uydurma riski yüksek
            continue
        if rconf >= 0.5 and len(re.findall(r"\w", text)) >= 2:
            lines.append({"text": text, "y": round((by0 + by1) / 2 / H, 3), "h": round(h / H, 3),
                          "x0": bx0 / W, "x1": bx1 / W, "conf": conf})
    return reading_order(lines)


OCR_WORKERS = int(os.environ.get("VIDNOTE_OCR_WORKERS") or max(1, min(4, (os.cpu_count() or 2) // 2)))
_ocr_local = threading.local()


def thread_detector():
    """Her iş parçacığına kendi metin dedektörü (aynı nesne eşzamanlı kullanılmaz)."""
    if not hasattr(_ocr_local, "det"):
        from rapidocr_onnxruntime import RapidOCR
        _ocr_local.det = RapidOCR()
    return _ocr_local.det


def ocr_many(paths, langs, engines):
    """Birden çok kareyi okur; çekirdek sayısı elveriyorsa paralel."""
    detector, have_tess, name = engines
    if name in ("kapalı", "yok"):
        return [[] for _ in paths]
    if OCR_WORKERS <= 1 or len(paths) < 2 or detector is None:
        return [ocr_frame(f, langs, detector, have_tess) for f in paths]
    with ThreadPoolExecutor(OCR_WORKERS) as ex:
        return list(ex.map(lambda f: ocr_frame(f, langs, thread_detector(), have_tess), paths))


def good_line(text):
    """OCR çöpünü ayıklar: birbirine yapışmış ya da sesli harf içermeyen 'kelimeler'den oluşan satırlar."""
    toks = text.split()
    if not toks:
        return False
    bad = sum(1 for t in toks if len(t) > 22 or (len(t) >= 5 and not re.search(r"[aeıioöuüAEIİOÖUÜ0-9]", t)))
    return bad / len(toks) <= 0.34


def reading_order(lines):
    """Satırları okuma sırasına dizer. Yan yana iki blok varsa (iki kapak, iki sütun)
    önce soldakini, sonra sağdakini okur; yoksa yukarıdan aşağıya."""
    cols, spanning = [], []
    for ln in sorted(lines, key=lambda l: l["x1"] - l["x0"]):          # dar satırlar sütunları kurar
        w = ln["x1"] - ln["x0"]
        hits = [c for c in cols
                if min(c["x1"], ln["x1"]) - max(c["x0"], ln["x0"]) > 0.3 * min(w, c["x1"] - c["x0"])]
        if w >= 0.6 or len(hits) >= 2:
            spanning.append(ln)                                         # sütunları aşan satır (başlık, altyazı)
        elif hits:
            c = hits[0]
            c["lines"].append(ln)
            c["x0"], c["x1"] = min(c["x0"], ln["x0"]), max(c["x1"], ln["x1"])
        else:
            cols.append({"x0": ln["x0"], "x1": ln["x1"], "lines": [ln]})
    by_y = lambda l: l["y"]
    if sum(len(c["lines"]) >= 2 for c in cols) < 2:
        return sorted(lines, key=by_y)
    top = min(l["y"] for c in cols for l in c["lines"])
    out = sorted([l for l in spanning if l["y"] < top], key=by_y)
    for c in sorted(cols, key=lambda c: c["x0"]):
        out += sorted(c["lines"], key=by_y)
    return out + sorted([l for l in spanning if l["y"] >= top], key=by_y)


def scan_text(path, duration, interval, tmp, langs, engines):
    """Videoyu düzenli aralıklarla tarayıp ekrandaki yazıların tamamını toplar.
    Kare seçimi görüntü değişimine bakar; bu tarama ise yazıya bakar: hareketli çekimin
    üstüne gelen küçük bir yazı ya da akıp giden altyazılar burada yakalanır."""
    step = max(interval, duration / 600)                 # çok uzun videoda en fazla ~600 örnek
    d = tmp / "scan"
    d.mkdir()
    ff(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", f"fps=1/{step:.3f}," + FRAME_SCALE.format(960),
        "-q:v", "3", str(d / "s_%05d.jpg")])
    files = sorted(d.glob("s_*.jpg"))
    smalls = [np.asarray(Image.open(f).convert("RGB").resize((AW, AH), Image.BOX)).astype(np.int16) for f in files]
    # Bir önceki örnekle aynı görünen kareler yeniden okunmaz
    need = [i for i in range(len(files)) if i == 0 or changed_fraction(smalls[i], smalls[i - 1]) >= LOCAL_THR / 2]
    read = dict(zip(need, ocr_many([files[i] for i in need], langs, engines)))
    items, last = [], []
    for i in range(len(files)):
        last = read.get(i, last)
        items.append({"t": i * step, "t0": i * step, "reason": "scan", "ocr": [dict(ln) for ln in last]})
    return items, step


def text_streams(scan, kept, per_entry=8):
    """Altyazı olmayan ekran yazıları ve altyazılar için iki ayrı akış çıkarır.
    Ekran yazılarında her satır bir kez listelenir; seçilen karelerdeki (daha yüksek çözünürlüklü)
    okuma önceliklidir. Okunaksız satırlar atılır; kalabalık ekranda en büyük yazılar öne alınır."""
    seen, screen = [], []
    def unseen(key):
        return len(key) >= 3 and not difflib.get_close_matches(key, seen, n=1, cutoff=0.7)
    def entry(t, lines):                                  # lines: ln sözlükleri (metin + boy)
        lines = [ln for ln in lines if good_line(ln["text"])]
        if not lines:
            return
        big = sorted(lines, key=lambda ln: -(ln.get("h") or 0))[:per_entry]
        keep = [ln["text"] for ln in lines if any(ln is b for b in big)]
        screen.append({"t": t, "lines": keep, "more": len(lines) - len(keep)})
    for k in kept:
        new = [ln for ln in k.get("ocr", []) if ln["text"] in k["text_new"] and unseen(ln["key"])]
        seen += [ln["key"] for ln in k.get("ocr", [])]
        entry(k["t0"], new)
    for it in scan or []:
        new = []
        for ln in it["ocr"]:
            if ln["text"] in it["text_new"] and unseen(ln["key"]):
                new.append(ln)
                seen.append(ln["key"])
        entry(it["t"], new)
    screen.sort(key=lambda x: x["t"])
    captions, prev = [], []
    for it in scan or kept:
        keys = {ln["text"]: ln["key"] for ln in it.get("ocr", [])}
        conf = {ln["text"]: ln.get("conf") or 0 for ln in it.get("ocr", [])}
        fresh = [c for c in it["text_caption"] if not difflib.get_close_matches(keys.get(c, c), prev, n=1, cutoff=0.8)]
        if fresh:
            captions.append({"t": it["t0"], "lines": fresh, "trust": [conf.get(c, 0) >= 75 for c in fresh]})
        prev = [keys.get(c, c) for c in it["text_caption"]]
    return screen, captions


def _norm1(w):
    w = unicodedata.normalize("NFKD", w.casefold())
    return "".join(c for c in w if c.isalnum())


def _align_words(xs, ys):
    """İki kelime dizisini benzerliğe göre hizalar (ör. "houtusu" ~ "howtosu").
    ("pair", i, j) / ("asr", i, None) / ("cap", None, j) adımlarını sırayla döndürür."""
    n, m = len(xs), len(ys)
    sim = [[difflib.SequenceMatcher(None, x, y).ratio() if x and y else 0.0 for y in ys] for x in xs]
    S = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            hit = S[i - 1][j - 1] + (sim[i - 1][j - 1] if sim[i - 1][j - 1] >= 0.5 else -1.0)
            S[i][j] = max(hit, S[i - 1][j], S[i][j - 1])
    ops, i, j = [], n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and sim[i - 1][j - 1] >= 0.5 and S[i][j] == S[i - 1][j - 1] + sim[i - 1][j - 1]:
            ops.append(("pair", i - 1, j - 1)); i -= 1; j -= 1
        elif i > 0 and S[i][j] == S[i - 1][j]:
            ops.append(("asr", i - 1, None)); i -= 1
        else:
            ops.append(("cap", None, j - 1)); j -= 1
    return ops[::-1]


def fuse_speech(transcript, captions):
    """Konuşma dökümünü ekrandaki altyazılarla hizalayıp düzeltir.
    Altyazı videoyu yapanın yazdığı metindir; konuşma tanımanın yanlış duyduğu yerde altyazının
    yazımı alınır. Altyazının kaçırdığı kelimeler konuşma dökümünden korunur. (yeni döküm, düzeltme sayısı)"""
    if not transcript or not captions:
        return transcript, 0
    a = [(w, si) for si, seg in enumerate(transcript) for w in seg["text"].split()]
    b, ok = [], []
    for c in captions:
        for n, line in enumerate(c["lines"]):
            ws = line.split()
            b += ws
            ok += [bool((c.get("trust") or [True] * len(c["lines"]))[n])] * len(ws)
    if not a or not b or len(a) * len(b) > 4e7:
        return transcript, 0
    na, nb = [_norm1(w) for w, _ in a], [_norm1(w) for w in b]
    sm = difflib.SequenceMatcher(None, na, nb, autojunk=False)
    out, fixed = [[] for _ in transcript], 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        la, lb = i2 - i1, j2 - j1
        if tag == "equal":                                           # aynı kelime: güvenilir okunduysa altyazının yazımı
            for k in range(la):
                out[a[i1 + k][1]].append(b[j1 + k] if ok[j1 + k] else a[i1 + k][0])
        elif tag == "insert":
            # Konuşma tanımanın atladığı kelimeler: iki eşleşme arasında, kısa ve net okunmuşsa eklenir
            if 0 < i1 < len(a) and lb <= 4 and all(ok[j1:j2]):
                out[a[i1 - 1][1]] += b[j1:j2]
                fixed += 1
        elif tag == "replace" and la == lb <= 3 and all(ok[j1:j2]):   # kısa ve net: altyazı konuşmanın yerine geçer
            for k in range(la):
                out[a[i1 + k][1]].append(b[j1 + k])
            fixed += 1
        elif tag == "replace" and la * lb <= 250000:                # farklı yazılmış bölge: kelime kelime eşle
            last_seg = a[i1][1]
            ops = _align_words(na[i1:i2], nb[j1:j2])
            for n, (kind, x, y) in enumerate(ops):
                if kind == "pair":                                  # aynı kelimenin iki yazımı
                    last_seg = a[i1 + x][1]
                    use = ok[j1 + y]                                # altyazı net okunduysa onu al, yoksa konuşma kalır
                    out[last_seg].append(b[j1 + y] if use else a[i1 + x][0])
                    fixed += use and _norm1(a[i1 + x][0]) != _norm1(b[j1 + y])
                elif kind == "asr":                                 # altyazıda yok: konuşma dökümü kalır
                    last_seg = a[i1 + x][1]
                    out[last_seg].append(a[i1 + x][0])
                else:
                    # Yalnızca altyazıda olan kelime: konuşma tanıma atlamış olabilir. Kısa, net okunmuş ve
                    # iki eşleşmenin arasındaysa eklenir; baştaki/sondaki uzun yazılar (başlık gibi) eklenmez.
                    lo = n
                    while lo > 0 and ops[lo - 1][0] == "cap":
                        lo -= 1
                    hi = n
                    while hi + 1 < len(ops) and ops[hi + 1][0] == "cap":
                        hi += 1
                    if ok[j1 + y] and hi - lo < 4 and lo > 0 and hi + 1 < len(ops):
                        out[last_seg].append(b[j1 + y])
        elif tag in ("delete", "replace"):                          # altyazıda yok: konuşma dökümü kalır
            for k in range(i1, i2):
                out[a[k][1]].append(a[k][0])
    return [dict(seg, text=" ".join(out[i]) or seg["text"]) for i, seg in enumerate(transcript)], fixed


def build_blocks(kept, transcript, screen, motions, fps, duration):
    """Dökümü zaman aralıklarına böler: her aralıkta o andaki konuşma, ekranda yeni beliren yazılar,
    ilgili kareler ve görüntünün ne kadar hareketli olduğu bulunur."""
    blocks = []
    for k in kept:
        gap = k["t0"] - blocks[-1]["start"] if blocks else 0
        if not blocks or gap >= 6 or (k["reason"] == "scene" and gap >= 2):
            blocks.append({"start": k["t0"], "frames": [k["n"]]})
        else:
            blocks[-1]["frames"].append(k["n"])
    for i, b in enumerate(blocks):
        b["end"] = blocks[i + 1]["start"] if i + 1 < len(blocks) else max(duration, b["start"])
        lo = -1 if i == 0 else b["start"]
        b["speech"] = []
        lines, more = [], 0
        for x in screen or []:
            if lo <= x["t"] < b["end"]:
                lines += x["lines"]
                more += x.get("more", 0)
        b["screen"], b["more"] = lines[:14], more + max(0, len(lines) - 14)
        seg = motions[int(b["start"] * fps) + 1: int(b["end"] * fps)]
        moving = sum(x >= 0.01 for x in seg) / len(seg) if seg else 0      # kesmeler değil, süregelen hareket
        b["motion"] = "sürekli hareket (kamera ya da sayfa kaydırma)" if moving >= 0.5 else ""
    # Konuşma bölümleri uzun olabilir: kelimeleri bölümün süresine yayıp ait oldukları aralığa koy
    for seg in transcript:
        words = seg["text"].split()
        for n, w in enumerate(words):
            tw = seg["start"] + (seg["end"] - seg["start"]) * n / max(len(words), 1)
            target = next((b for b in blocks if b["start"] <= tw < b["end"]), blocks[-1] if tw >= blocks[-1]["start"] else blocks[0])
            target["speech"].append(w)
    for b in blocks:
        b["speech"] = " ".join(b["speech"])
    return blocks


def frames_for_new_text(path, scan, step, kept, revisits, tmp, langs, detector, have_tess, limit=15):
    """Taramada görülen ama hiçbir seçilmiş karede olmayan kalıcı yazılar için kare ekler."""
    known = [ln["key"] for k in kept for ln in k.get("ocr", [])]
    times = []
    for n, it in enumerate(scan):
        nxt = [ln["key"] for ln in scan[n + 1]["ocr"]] if n + 1 < len(scan) else []
        cand = [ln for ln in it["ocr"] if ln["text"] in it["text_new"] and len(ln["key"]) >= 6
                and not difflib.get_close_matches(ln["key"], known, n=1, cutoff=0.75)
                and difflib.get_close_matches(ln["key"], nxt, n=1, cutoff=0.75)]      # bir sonraki örnekte de duruyor
        near = any(abs(k["t"] - it["t"]) <= 1.5 for k in kept)            # o an için zaten bir kare var
        if cand and not near and len(times) < limit:
            times.append(it["t"])
            known += [ln["key"] for ln in it["ocr"]]
    if not times:
        return 0
    for r in revisits:
        r["obj"] = kept[r["of"]]
    for n, t in enumerate(times):
        f = tmp / "raw" / f"extra_{n:03d}.jpg"
        ff(["ffmpeg", "-v", "error", "-y", "-ss", f"{t + min(0.3, step / 2):.2f}", "-i", path, "-frames:v", "1",
            "-vf", FRAME_SCALE.format(1280), "-q:v", "2", str(f)])
        if f.exists():
            kept.append({"t": t + min(0.3, step / 2), "t0": t, "reason": "text", "score": 1.0, "settled": True, "raw": f,
                         "ocr": ocr_frame(f, langs, detector, have_tess)})
    kept.sort(key=lambda k: k["t0"])
    for r in revisits:
        r["of"] = kept.index(r.pop("obj"))
    return len(times)


def norm_words(s):
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.findall(r"\w+", s)


def _said(word, spoken):
    """Kelime konuşmada geçiyor mu? Konuşma tanıma hatalarına karşı yaklaşık eşleşme."""
    if word in spoken:
        return True
    return len(word) >= 4 and bool(difflib.get_close_matches(word, spoken, n=1, cutoff=0.8))


def classify_text(kept, transcript):
    """Her karede: konuşmanın tekrarı olan altyazıları ve önceki karede zaten
    görünen satırları ayıklar; geriye yalnızca *yeni* ekran bilgisi kalır."""
    # 1. geçiş: konuşmayla eşleşen satırlar = altyazı
    for n, k in enumerate(kept):
        t0 = k["t0"] - 6
        t1 = (kept[n + 1]["t0"] if n + 1 < len(kept) else k["t"] + 30) + 2
        spoken = set()
        for s in transcript:
            if s["end"] >= t0 and s["start"] <= t1:
                spoken.update(norm_words(s["text"]))
        spoken = list(spoken)
        for ln in k.get("ocr", []):
            words = [w for w in norm_words(ln["text"]) if len(w) >= 2]
            ln["key"] = "".join(words)
            ln["caption"] = (len(words) >= 2 and len(ln["key"]) >= 8
                             and sum(_said(w, spoken) for w in words) / len(words) >= 0.7)
    # 2. geçiş: altyazıların ekrandaki yerini öğren; o banttaki diğer satırlar da altyazıdır
    caps = sorted((ln["y"], ln["h"]) for k in kept for ln in k.get("ocr", []) if ln["caption"] and ln["y"] is not None)
    clusters = []
    for y, h in caps:                              # altyazılar birden fazla yükseklikte olabilir
        if clusters and y - clusters[-1][0][0] <= 0.05:
            clusters[-1].append((y, h))
        else:
            clusters.append([(y, h)])
    # bant = (ortalama konum, tipik yazı boyu); en az iki eşleşmiş altyazı gerekir
    bands = [(float(np.mean([y for y, _ in c])), float(np.median([h for _, h in c]))) for c in clusters if len(c) >= 2]
    counts = {}
    for k in kept:
        for key in {ln["key"] for ln in k.get("ocr", [])}:
            counts[key] = counts.get(key, 0) + 1
    prev_lines = []
    for k in kept:
        k["text_new"], k["text_caption"] = [], []
        cur = set()
        for ln in k.get("ocr", []):
            cur.add(ln["key"])
            in_band = ln["y"] is not None and any(
                abs(ln["y"] - by) <= 0.04 and 0.7 <= ln["h"] / bh <= 1.4 for by, bh in bands)
            if ln["caption"] or (in_band and counts[ln["key"]] <= 2):
                k["text_caption"].append(ln["text"])
            elif not difflib.get_close_matches(ln["key"], prev_lines, n=1, cutoff=0.8):
                k["text_new"].append(ln["text"])
        prev_lines = list(cur)
    return bands


# ---- 3) konuşma -----------------------------------------------------------
def extract_audio(path, wav):
    ff(["ffmpeg", "-v", "error", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])


def asr_faster_whisper(wav, model, lang, device="cpu"):
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device=device, compute_type="int8" if device == "cpu" else "default")
    segs, info = m.transcribe(str(wav), language=lang, vad_filter=True)
    out = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segs]
    return [s for s in out if s["text"]], info.language


def asr_mlx(wav, model, lang):
    """Apple Silicon Mac'lerde GPU ile çalışan whisper (mlx-whisper)."""
    import mlx_whisper
    repo = model if "/" in model else f"mlx-community/whisper-{model}"
    r = mlx_whisper.transcribe(str(wav), path_or_hf_repo=repo, language=lang)
    out = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in r["segments"]]
    return [s for s in out if s["text"]], r.get("language")


def asr_sherpa(wav, model_dir, vad_model, lang):
    import sherpa_onnx
    d = Path(model_dir)
    pick = lambda pat: (sorted(d.glob(pat + ".int8.onnx")) or sorted(d.glob(pat + ".onnx")))[0]
    rec = sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=str(pick("*encoder*")), decoder=str(pick("*decoder*")),
        tokens=str(sorted(d.glob("*tokens*.txt"))[0]), language=lang or "", task="transcribe", num_threads=2)
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0

    def decode(chunk):
        st = rec.create_stream()
        st.accept_waveform(sr, chunk)
        rec.decode_stream(st)
        return st.result.text.strip()

    out = []
    if vad_model:                                   # sessizlikleri atla, konuşma parçalarını zaman damgala
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(vad_model)
        cfg.silero_vad.min_silence_duration = 0.7
        cfg.silero_vad.max_speech_duration = 20
        cfg.sample_rate = sr
        vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=120)
        win = cfg.silero_vad.window_size

        def drain():
            while not vad.empty():
                seg = vad.front
                chunk = np.array(seg.samples, dtype=np.float32)
                text = decode(chunk)
                if text:
                    out.append({"start": seg.start / sr, "end": seg.start / sr + len(chunk) / sr, "text": text})
                vad.pop()

        for p in range(0, len(samples), win):
            vad.accept_waveform(samples[p:p + win])
            drain()
        vad.flush()
        drain()
    else:                                           # VAD yoksa 25 sn'lik sabit parçalar
        step = 25 * sr
        for p in range(0, len(samples), step):
            chunk = samples[p:p + step]
            if np.abs(chunk).max() < 0.01:
                continue
            text = decode(chunk)
            if text:
                out.append({"start": p / sr, "end": (p + len(chunk)) / sr, "text": text})
    return out, lang or "otomatik"


def transcribe(wav, args):
    if args.reuse_transcript:      # önceki bir pack.json'daki transkripti yeniden kullan
        old = json.loads(Path(args.reuse_transcript).read_text(encoding="utf-8"))
        return old["transcript"], old["meta"].get("language"), old["meta"].get("asr", "hazır transkript")
    if args.asr == "none":
        return [], None, "kapalı"
    if args.asr in ("auto", "sherpa") and args.asr_model_dir:
        segs, lang = asr_sherpa(wav, args.asr_model_dir, args.vad_model, args.lang)
        return segs, lang, f"sherpa-onnx ({Path(args.asr_model_dir).name})"
    engines = []
    if args.asr in ("auto", "mlx") and sys.platform == "darwin" and platform.machine() == "arm64":
        engines.append(("mlx-whisper", asr_mlx))
    if args.asr in ("auto", "faster-whisper"):
        engines.append(("faster-whisper", asr_faster_whisper))
    for name, fn in engines:
        try:
            extra = (args.whisper_device,) if fn is asr_faster_whisper else ()
            segs, lang = fn(wav, args.whisper_model, args.lang, *extra)
            return segs, lang, f"{name} ({args.whisper_model})"
        except Exception as e:  # kütüphane yok, model indirilemedi vb. -> sıradakini dene
            log(f"uyarı: {name} çalışmadı ({type(e).__name__}: {str(e)[:200]})")
    log("uyarı: konuşma metne çevrilemedi. Transkriptsiz devam ediliyor.")
    return [], None, "başarısız"


# ---- 4) paket -------------------------------------------------------------
def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "C:/Windows/Fonts/arialbd.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


def make_sheets(kept, outdir, cols=3, tile=384, per_sheet=9, prefix="sheet"):
    """Kareleri zaman damgalı kontak baskılara dizer: aynı içerik, çok daha az görsel token."""
    outdir.mkdir(parents=True, exist_ok=True)
    sheets, fnt = [], font(22)
    for s in range(0, len(kept), per_sheet):
        group = kept[s:s + per_sheet]
        ims = []
        for k in group:
            im = Image.open(k["file"]).convert("RGB")
            im.thumbnail((tile, tile * 2))
            ims.append(im)
        th = max(im.height for im in ims)
        rows = -(-len(ims) // cols)
        sheet = Image.new("RGB", (min(cols, len(ims)) * tile, rows * th), "black")
        draw = ImageDraw.Draw(sheet)
        for n, (k, im) in enumerate(zip(group, ims)):
            x, y = (n % cols) * tile, (n // cols) * th
            sheet.paste(im, (x + (tile - im.width) // 2, y))
            label = k.get("label") or f"#{k['n']}  {mmss(k['t0'])}"
            box = draw.textbbox((x + 6, y + 6), label, font=fnt)
            draw.rectangle((box[0] - 4, box[1] - 4, box[2] + 4, box[3] + 4), fill="black")
            draw.text((x + 6, y + 6), label, fill="yellow", font=fnt)
        p = outdir / f"{prefix}_{len(sheets) + 1:02d}.jpg"
        sheet.save(p, quality=85)
        sheets.append(p)
    return sheets


def img_tokens(path):
    w, h = Image.open(path).size
    return w * h / 750  # kaba tahmin; modele göre değişir


PROMPT = """Aşağıdaki paket bir videodan çıkarıldı: kronolojik anahtar kareler, karelerdeki ekran yazıları ve zaman damgalı konuşma.
Bunları birlikte okuyup videonun içeriğini bana not olarak çıkar:

1. **Tek cümlelik özet** — video ne anlatıyor?
2. **Ana noktalar** — maddeler hâlinde, her birinin yanında zaman damgası.
3. **Ekranda gösterilen bilgiler** — listeler, adımlar, fiyatlar, isimler, linkler, kodlar: eksiksiz ve yazıldığı gibi.
4. **Görsel olarak ne var** — konuşmada geçmeyen ama görüntüde olan şeyler (ürün, mekân, grafik, arayüz, hareket).
5. **Yapılacaklar / işime yarayacaklar** — varsa.
6. **Emin olunamayanlar** — okunamayan yazı, kareler arasında kaçmış olabilecek hareket.

Kurallar: "Ekran yazısı" satırları OCR'dan geldi ve hatalı olabilir; görseldeki yazıyla çelişirse görsele güven.
Karelerde ve konuşmada olmayan hiçbir şeyi ekleme.
"""


def build_pack(out, meta, kept, revisits, transcript, sheets, timings, screen_text=None, captions=None, comments=None):
    events = [("frame", k["t0"], k) for k in kept] + [("revisit", r["t"], r) for r in revisits]
    events.sort(key=lambda e: e[1])
    bounds = [k["t0"] for k in kept] + [float("inf")]

    text_chars = sum(len(s["text"]) for s in transcript) + sum(len(x) for k in kept for x in k["text_new"])
    tok_text = int(text_chars / 3.2) + 40 * len(kept)
    tok_frames = int(sum(img_tokens(k["file"]) for k in kept))
    tok_sheets = int(sum(img_tokens(s) for s in sheets))
    meta["tokens"] = {"text": tok_text, "frames": tok_frames, "sheets": tok_sheets}

    L = [f"# Video bağlam paketi — {meta['title']}", ""]
    L += [f"- Kaynak: {meta.get('webpage_url') or meta['source']}",
          f"- Süre: {mmss(meta['duration'])} · {meta['width']}x{meta['height']}",
          f"- Kare: {len(kept)} seçildi / {meta['analyzed']} tarandı ({meta['fps']} kare/sn)"
          + (f" · {meta['pruned_captions']} altyazı karesi elendi" if meta["pruned_captions"] else ""),
          f"- Konuşma: {len(transcript)} bölüm · motor: {meta['asr']}" + (f" · dil: {meta['language']}" if meta.get("language") else ""),
          f"- Ekran yazısı: {meta['ocr']}",
          f"- Yaklaşık boyut: metin ~{tok_text} token · tek tek kareler ~{tok_frames} token · kontak baskı ~{tok_sheets} token",
          ""]
    if meta.get("description"):
        L += ["## Açıklama (yayıncının yazdığı)", "", meta["description"].strip()[:1500], ""]
    L += ["## Okuma notu", "",
          "Kareler kronolojik. Bir kare, bir sonraki kareye kadar ekranda olanı temsil eder. "
          "\"Ekran yazısı\" yalnızca o karede *yeni* beliren ve konuşmanın tekrarı olmayan yazıları listeler (OCR; hatalı olabilir). "
          "Kareler arasındaki hareket pakette yoktur.", "",
          "## Zaman çizelgesi", ""]
    for kind, t, e in events:
        if kind == "revisit":
            L += [f"↩ **{mmss(t)}** — Kare {kept[e['of']]['n']}'deki görüntüye geri dönülüyor (kare tekrarlanmadı).", ""]
            continue
        n = e["n"]
        L += [f"### {mmss(t)} — Kare {n} ({LABEL[e['reason']]})", "", f"![Kare {n}](frames/{Path(e['file']).name})", ""]
        if e["text_new"]:
            L += ["**Ekran yazısı:**"] + [f"- {x}" for x in e["text_new"]] + [""]
        if e["text_caption"]:
            L += ["_Ekrandaki altyazı (konuşmanın yazılı hâli):_ " + " / ".join(e["text_caption"]), ""]
        said = [s for s in transcript if bounds[n - 1] <= s["start"] < bounds[n]]
        if n == 1:
            said = [s for s in transcript if s["start"] < bounds[1]]
        if said:
            L += ["**Konuşma:**"] + [f"- [{mmss(s['start'])}–{mmss(s['end'])}] {s['text']}" for s in said] + [""]
    if sheets:
        L += ["## Kontak baskılar", "", "Kareleri tek tek yüklemek yerine bunları vermek görsel token'ı düşürür.", ""]
        L += [f"![{p.name}](sheets/{p.name})" for p in sheets] + [""]
    (out / "pack.md").write_text("\n".join(L), encoding="utf-8")
    (out / "prompt.md").write_text(PROMPT, encoding="utf-8")
    (out / "transcript.txt").write_text(
        "\n".join(f"[{mmss(s['start'])}] {s['text']}" for s in transcript), encoding="utf-8")
    js = {"meta": meta, "timings_sec": timings,
          "frames": [{"n": k["n"], "t": round(k["t"], 2), "shown_from": round(k["t0"], 2), "time": mmss(k["t0"]), "reason": k["reason"],
                      "file": f"frames/{Path(k['file']).name}", "ocr_all": [ln["text"] for ln in k.get("ocr", [])],
                      "text_new": k["text_new"], "text_caption": k["text_caption"]} for k in kept],
          "revisits": [{"t": round(r["t"], 2), "of_frame": kept[r["of"]]["n"]} for r in revisits],
          "screen_text": screen_text, "captions": captions, "comments": comments or [],
          "transcript": [{"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"]} for s in transcript]}
    (out / "pack.json").write_text(json.dumps(js, ensure_ascii=False, indent=2), encoding="utf-8")


def doc_head(meta):
    """Dökümün başı: başlık, kaynak bilgileri ve açıklama."""
    L = [f"# {meta['title']}", ""]
    L.append(f"- Kaynak: {meta.get('webpage_url') or meta['source']}")
    if meta.get("uploader"):
        L.append(f"- Hesap: {meta['uploader']}")
    if meta.get("upload_date"):
        L.append(f"- Yayın tarihi: {meta['upload_date']}")
    if meta.get("duration"):
        L.append(f"- Süre: {mmss(meta['duration'])}")
    elif meta.get("kind") == "text":
        L.append("- Tür: yalnızca metin gönderisi")
    else:
        L.append(f"- Tür: görsel gönderi ({meta['frames']} görsel)")
    st = meta.get("stats") or {}
    if st:
        names = (("plays", "izlenme"), ("likes", "beğeni"), ("comments", "yorum"))
        L.append("- Etkileşim: " + " · ".join(f"{int(st[k]):,} {tr}".replace(",", ".") for k, tr in names if k in st))
    L.append(f"- İşlenme: {meta['created']}" + (f" · konuşma: {meta['asr']}" if meta.get("duration") else "")
             + (f" · dil: {meta['language']}" if meta.get("language") else ""))
    L += ["", "## Açıklama", ""]
    L += [meta["description"].strip() if meta.get("description", "").strip() else "_Açıklama yok._", ""]
    return L


def comment_section(comments):
    if not comments:
        return []
    def line(c, indent=""):
        who = "gönderi sahibi" if c.get("by_owner") else (f"@{c['user']}" if c.get("user") else "")
        likes = f"{c['likes']} beğeni" if c.get("likes") else ""
        tag = ", ".join(x for x in (who, likes) if x)
        return f"{indent}- " + (f"({tag}) " if tag else "") + " ".join(str(c["text"]).split())
    L = [f"## Yorumlar ({len(comments)})", "",
         "_Gönderinin altındaki yorumlar; içeriğin kendisi değildir, izleyenlerin yazdıklarıdır._", ""]
    for c in comments:
        L.append(line(c))
        L += [line(r, "  ") for r in (c.get("replies") or []) if isinstance(r, dict) and str(r.get("text", "")).strip()]
    return L + [""]


def ocr_engines(args):
    """(metin kutusu dedektörü, tesseract var mı, görünen ad) döndürür."""
    if args.no_ocr:
        return None, False, "kapalı"
    have_tess = bool(shutil.which("tesseract"))
    try:
        from rapidocr_onnxruntime import RapidOCR
        detector = RapidOCR()
    except Exception:
        detector = None
    if detector is None and not have_tess:
        log("uyarı: OCR motoru yok (pip install rapidocr-onnxruntime ve/veya tesseract kurun).")
        return None, False, "yok"
    return detector, have_tess, " + ".join(x for x in ["metin kutusu tespiti" if detector else "", "tesseract" if have_tess else ""] if x)


def prepare_images(paths, out, tmp, args, engines):
    """Görselleri pakete alır ve üstlerindeki yazıları okur."""
    detector, have_tess, name = engines
    items = []
    for p in paths:
        try:
            im = Image.open(p).convert("RGB")
        except Exception as e:
            log(f"uyarı: görsel açılamadı ({Path(p).name}): {e}")
            continue
        n = len(items) + 1
        full = im.copy()
        full.thumbnail((1600, 1600))
        fp = Path(tmp) / f"img_full_{n:03d}.jpg"
        full.save(fp, quality=92)
        lines = ocr_frame(fp, args.ocr_lang, detector, have_tess) if name not in ("kapalı", "yok") else []
        im.thumbnail((args.frame_size, args.frame_size))
        f = out / "frames" / f"g{n:03d}.jpg"
        im.save(f, quality=85)
        items.append({"n": n, "t": 0.0, "t0": 0.0, "reason": "image", "file": f, "label": f"Görsel {n}",
                      "ocr": lines, "text": [ln["text"] for ln in lines]})
    return items


def run_images(args, images, src_meta, comments, out, tmp, say, t_all):
    """Videosuz gönderi (tek ya da çoklu görsel): açıklama + görsellerdeki yazılar + görseller."""
    say("Görsellerdeki yazılar okunuyor")
    engines = ocr_engines(args)
    items = prepare_images(images, out, tmp, args, engines)
    if not items:
        raise VidnoteError("Görseller açılamadı.")
    sheets = make_sheets(items, out / "sheets")
    meta = dict(src_meta, source=args.source_label or args.input, duration=0, frames=len(items), kind="images",
                asr="yok", ocr=engines[2], language=None, created=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    L = doc_head(meta)
    L += ["## Görsellerdeki yazılar", ""]
    L += [f"- {k['label']}: " + (" / ".join(k["text"]) if k["text"] else "_yazı yok_") for k in items]
    L += ["", "_Yazılar otomatik okundu; hatalı olabilir. Görselde ne olduğu yalnızca görselin kendisinden anlaşılır._", ""]
    L += comment_section(comments)
    names = ", ".join(f"`sheets/{p.name}`" for p in sheets)
    L += ["## Görseller", "", f"{len(items)} görsel; numaralarıyla şu dosyalarda: {names}. Tek tek: `frames/` klasörü. "
          "Görüntüde ne olduğunu anlamak için bu dosyaları da modele verin.", ""]
    tpath = out / "transkript.md"
    tpath.write_text("\n".join(L), encoding="utf-8")
    timings = {"toplam": round(time.time() - t_all, 2)}
    (out / "pack.json").write_text(json.dumps({
        "meta": meta, "timings_sec": timings, "comments": comments or [],
        "images": [{"n": k["n"], "file": f"frames/{k['file'].name}", "text": k["text"]} for k in items]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"bitti: {tpath}  ({len(items)} görsel, {timings['toplam']} sn)")
    return {"out": out, "transcript": tpath, "sheets": sheets, "meta": meta, "timings": timings}


def run_text_post(args, src_meta, comments, out, say, t_all, notes=None):
    """Yalnızca metin içeren gönderi (ör. Threads media_type 19): açıklama + yorumlar; görsel/video yok."""
    say("Döküm hazırlanıyor")
    meta = dict(src_meta, source=args.source_label or args.input, duration=0, frames=0, kind="text",
                asr="yok", ocr="yok", language=None, created=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    L = doc_head(meta)
    L += comment_section(comments)
    for n in notes or []:
        L += [f"_{n}_", ""]
    tpath = out / "transkript.md"
    tpath.write_text("\n".join(L), encoding="utf-8")
    timings = {"toplam": round(time.time() - t_all, 2)}
    (out / "pack.json").write_text(json.dumps(
        {"meta": meta, "timings_sec": timings, "comments": comments or []},
        ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"bitti: {tpath}  (yalnız metin, {timings['toplam']} sn)")
    return {"out": out, "transcript": tpath, "sheets": [], "meta": meta, "timings": timings}


def write_transcript(out, meta, kept, blocks, sheets, comments=None, extra_images=None, notes=None, fixed=0):
    """Tek dosyada, modele verilmeye hazır döküm. Özet değildir; videodaki bilgiyi tekrarsız bir zaman akışına dizer."""
    L = doc_head(meta)
    L += ["## Zaman akışı", "",
          "_Her bölüm bir zaman aralığıdır: o aralıktaki konuşma, ekranda ilk kez görünen yazılar ve görüntünün hareketi."
          + (f" Konuşma metni ekrandaki altyazılarla karşılaştırılıp {fixed} yerde düzeltildi." if fixed else "")
          + " Ekran yazıları otomatik okundu; hatalı olabilir._", ""]
    for b in blocks:
        fr = b["frames"]
        L += [f"### {mmss(b['start'])}–{mmss(b['end'])} · Kare {fr[0]}" + (f"–{fr[-1]}" if len(fr) > 1 else ""), ""]
        if b["speech"]:
            L += [f"**Konuşma:** {b['speech']}", ""]
        if b["screen"]:
            L += ["**Ekranda yazan:** " + " / ".join(b["screen"])
                  + (f" _(+{b['more']} satır daha; karelerde)_" if b["more"] else ""), ""]
        if b["motion"]:
            L += [f"**Görüntü:** {b['motion']}", ""]
        if not (b["speech"] or b["screen"] or b["motion"]):
            L += ["_Yeni görüntü; konuşma ve yazı yok._", ""]
    if not meta.get("has_speech"):
        L += ["_Bu videoda konuşma bulunamadı ya da çözülemedi._", ""]
    L += comment_section(comments)
    if extra_images:
        L += ["## Gönderideki görseller", "", "_Videonun yanında paylaşılan görseller ve üstlerindeki yazılar._", ""]
        L += [f"- {k['label']}: " + (" / ".join(k["text"]) if k["text"] else "_yazı yok_") for k in extra_images] + [""]
    for n in notes or []:
        L += [f"_{n}_", ""]
    L += ["## Görseller", ""]
    if sheets:
        names = ", ".join(f"`sheets/{p.name}`" for p in sheets)
        L += [f"{len(kept)} kare seçildi; numaraları ve zaman damgalarıyla şu dosyalarda: {names}. "
              "Görüntüde ne olduğunu anlamak için bu dosyaları da modele verin.", ""]
    L += ["Karelerin zamanı: " + ", ".join(f"{k['n']}={mmss(k['t0'])}" for k in kept), ""]
    path = out / "transkript.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def build_parser():
    ap = argparse.ArgumentParser(description="Videoyu yapay zekânın okuyabileceği bir döküme çevirir.")
    ap.add_argument("input", help="video ya da görsel dosyası, link ya da VidNote paketi (.zip)")
    ap.add_argument("-o", "--out", help="çıktı klasörü (varsayılan: <video adı>_pack)")
    ap.add_argument("--max-frames", type=int, default=60, help="en fazla kare (varsayılan 60)")
    ap.add_argument("--frame-size", type=int, default=768, help="pakete giren karenin uzun kenarı (px)")
    ap.add_argument("--lang", help="konuşma dili (tr, en ...). Boşsa otomatik")
    ap.add_argument("--asr", choices=["auto", "mlx", "faster-whisper", "sherpa", "none"], default="auto")
    ap.add_argument("--whisper-model", default="large-v3-turbo", help="whisper modeli (varsayılan large-v3-turbo)")
    ap.add_argument("--asr-model-dir", help="sherpa-onnx whisper model klasörü (çevrimdışı kullanım)")
    ap.add_argument("--vad-model", help="silero_vad.onnx yolu (sherpa ile zaman damgası için)")
    ap.add_argument("--reuse-transcript", help="önceki bir çalıştırmanın pack.json dosyası; konuşmayı yeniden çözmez")
    ap.add_argument("--ocr-lang", default="tur+eng", help="tesseract dilleri (varsayılan tur+eng)")
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--text-scan", type=float, default=1.0,
                    help="ekrandaki tüm yazılar için tarama aralığı, saniye (varsayılan 1; 0 = kapalı, daha hızlı)")
    ap.add_argument("--keep-captions", action="store_true", help="yalnızca altyazı değişimi olan kareleri eleme")
    ap.add_argument("--cookies-from-browser", help="giriş isteyen siteler için tarayıcı adı (chrome, firefox)")
    ap.add_argument("--cookies", help="giriş isteyen siteler için cookies.txt dosyası (sunucuda tarayıcı yoksa)")
    ap.add_argument("--whisper-device", default="cpu", choices=["cpu", "cuda", "auto"], help="faster-whisper için aygıt")
    ap.add_argument("--max-height", type=int, default=720,
                    help="indirilecek videonun en fazla yüksekliği (varsayılan 720; 360 veri ve disk kazandırır, küçük yazılar okunmayabilir)")
    ap.add_argument("--no-site-captions", action="store_true",
                    help="YouTube'un hazır altyazısını kullanma; konuşmayı her zaman baştan çöz")
    ap.add_argument("--comments", type=int, default=0,
                    help="linkten işlerken en fazla bu kadar yorum da al (varsayılan 0 = alma)")
    ap.add_argument("--title", help="başlığı elle ver")
    ap.add_argument("--source-label", help="dökümde 'Kaynak' olarak yazılacak ad (geçici dosya yolu yerine)")
    ap.add_argument("--description", help="açıklamayı elle ver (dosyadan işlerken)")
    return ap


def run(args, progress=None):
    """Bir videoyu işler; çıktı klasörünü ve özet bilgileri döndürür.
    progress: her aşamada (durum metni, varsa 0-1 arası oran) ile çağrılan işlev (arayüz için)."""
    say = progress or (lambda msg, fraction=None: None)
    timings, t_all = {}, time.time()
    tmp = Path(tempfile.mkdtemp(prefix="vidnote_"))
    try:
        t = time.time()
        src_meta = {"title": "", "description": "", "uploader": "", "upload_date": "", "webpage_url": "", "site": "", "stats": {}}
        comments, images, notes = [], [], []
        if re.match(r"https?://", args.input):
            url = args.input
            insta = bool(re.search(r"//(www\.)?instagram\.com/", url))
            src = None
            if THREADS_RE.search(url):                      # Threads: yt-dlp/gallery-dl denenmez, kendi çözücümüz
                say("Gönderi indiriliyor")
                log("threads gönderisi indiriliyor…")
                images, videos, src_meta = fetch_threads_post(url, tmp, args.cookies_from_browser, args.cookies)
                src = str(videos[0]) if videos else None
                if len(videos) > 1:
                    notes.append(f"Gönderide {len(videos)} video var; yalnızca ilki işlendi.")
                log(f"gönderi: {len(images)} görsel, {len(videos)} video")
            else:
                if not (insta and "/p/" in url):           # /p/ gönderileri görsel olabilir; onlar aşağıda
                    say("Video indiriliyor")
                    log("indiriliyor…")
                    try:
                        src, src_meta, comments = fetch_url(url, tmp, args.cookies_from_browser, args.cookies, args.comments, say,
                                                             args.max_height, not args.no_site_captions, args.lang)
                    except VidnoteError as e:
                        if not (insta and "no video in this post" in str(e).lower()):
                            raise
                if src is None:                             # görsel ya da çoklu Instagram gönderisi
                    say("Gönderi indiriliyor")
                    log("gönderi indiriliyor (gallery-dl)…")
                    images, videos, src_meta = fetch_instagram_post(url, tmp, args.cookies_from_browser, args.cookies)
                    src = str(videos[0]) if videos else None
                    if len(videos) > 1:
                        notes.append(f"Gönderide {len(videos)} video var; yalnızca ilki işlendi.")
                    log(f"gönderi: {len(images)} görsel, {len(videos)} video")
            default_out = re.sub(r"[^\w\-]+", "_", src_meta["title"])[:60].strip("_") or "video"
        else:
            src = args.input
            if not Path(src).exists():
                raise VidnoteError(f"Dosya bulunamadı: {src}")
            default_out = Path(src).stem
            if Path(src).suffix.lower() == ".zip":          # eklentinin ürettiği paket
                say("Paket açılıyor")
                src, images, src_meta, comments = open_package(src, tmp)
                log(f"paket: {len(images)} görsel, {len(comments)} yorum, açıklama {len(src_meta['description'])} karakter")
            elif Path(src).suffix.lower() in IMAGE_SUFFIXES:   # tek görsel (ekran görüntüsü de olabilir)
                images, src, src_meta["title"] = [Path(src)], None, Path(src).stem
            else:
                src_meta["title"] = Path(src).stem
        if args.title:
            src_meta["title"] = args.title
        if args.description:
            src_meta["description"] = args.description
        out = Path(args.out or f"{default_out}_pack")
        if out.exists():
            shutil.rmtree(out)
        (out / "frames").mkdir(parents=True)
        timings["girdi"] = round(time.time() - t, 2)
        if src is None:
            if not images:                                  # yalnız metin gönderisi (ör. Threads media_type 19)
                return run_text_post(args, src_meta, comments, out, say, t_all, notes)
            return run_images(args, images, src_meta, comments, out, tmp, say, t_all)

        info = probe(src)
        dur = info["duration"]
        fps = 4 if dur <= 600 else (2 if dur <= 3600 else 1)
        log(f"video: {mmss(dur)}, {info['width']}x{info['height']}")

        engines = ocr_engines(args)
        detector, have_tess, ocr_name = engines
        scanning = ocr_name not in ("kapalı", "yok") and args.text_scan > 0

        def visual():
            """Görüntü tarafı: kare seçimi ve yazı okuma. Konuşma çözülürken arka planda çalışır."""
            t0 = time.time()
            kept, revisits, analyzed, motions = select_frames(src, fps, dur, args.max_frames)
            for k, f in zip(kept, extract_frames(src, fps, kept, tmp / "raw")):
                k["raw"] = f
            timings["kare_secimi"] = round(time.time() - t0, 2)
            log(f"kare: {analyzed} tarandı → {len(kept)} seçildi, {len(revisits)} tekrar atlandı")
            t0 = time.time()
            for k, lines in zip(kept, ocr_many([k["raw"] for k in kept], args.ocr_lang, engines)):
                k["ocr"] = lines
            scan, step = scan_text(src, dur, args.text_scan, tmp, args.ocr_lang, engines) if scanning else (None, 0)
            timings["yazi_okuma"] = round(time.time() - t0, 2)
            return kept, revisits, analyzed, motions, scan, step

        say("Kareler seçiliyor, yazılar okunuyor, konuşma yazıya dökülüyor")
        pool = ThreadPoolExecutor(1)
        vis = pool.submit(visual)
        t = time.time()
        transcript, language, asr_name = [], None, "ses yok"
        ready = src_meta.pop("ready_transcript", None)
        if ready:                                           # sitenin hazır altyazısı: konuşmayı çözmeye gerek yok
            transcript, language, asr_name = ready
        elif info["has_audio"]:
            wav = tmp / "audio.wav"
            extract_audio(src, wav)
            transcript, language, asr_name = transcribe(wav, args)
        timings["konusma"] = round(time.time() - t, 2)
        log(f"konuşma: {len(transcript)} bölüm ({asr_name}, {timings['konusma']} sn)")
        say("Ekrandaki yazılar okunuyor")
        kept, revisits, analyzed, motions, scan, step = vis.result()
        pool.shutdown()

        t = time.time()
        classify_text(kept, transcript)
        added = 0
        if scan is not None:
            classify_text(scan, transcript)
            added = frames_for_new_text(src, scan, step, kept, revisits, tmp, args.ocr_lang, detector, have_tess)
            if added:
                classify_text(kept, transcript)
            log(f"yazı taraması: {len(scan)} örnek ({step:.1f} sn arayla), {added} ek kare")

        # Tek yeniliği konuşmanın altyazısı olan kareleri ele
        pruned = 0
        while not args.keep_captions:
            keep_idx = [n for n, k in enumerate(kept)
                        if n == 0 or not (k["reason"] == "local" and k["text_caption"] and not k["text_new"])]
            if len(keep_idx) == len(kept):
                break
            pruned += len(kept) - len(keep_idx)
            remap = {old: new for new, old in enumerate(keep_idx)}
            kept = [kept[n] for n in keep_idx]
            revisits = [dict(r, of=remap[r["of"]]) for r in revisits if r["of"] in remap]
            classify_text(kept, transcript)

        say("Döküm hazırlanıyor")
        screen_text, captions = text_streams(scan, kept)
        raw_transcript = transcript
        transcript, fixed = fuse_speech(transcript, captions)       # altyazıyla düzeltilmiş konuşma
        if fixed:
            log(f"konuşma, ekrandaki altyazılarla {fixed} yerde düzeltildi")
        for n, k in enumerate(kept, 1):
            k["n"] = n
            im = Image.open(k["raw"]).convert("RGB")
            im.thumbnail((args.frame_size, args.frame_size))
            k["file"] = out / "frames" / f"f{n:03d}_{mmss(k['t0']).replace(':', '-')}.jpg"
            im.save(k["file"], quality=82)
        sheets = make_sheets(kept, out / "sheets")
        extra = []
        if images:                                      # videonun yanında gelen görseller
            say("Gönderideki görseller okunuyor")
            extra = prepare_images(images, out, tmp, args, engines)
            sheets += make_sheets(extra, out / "sheets", prefix="gorsel")

        timings["toplam"] = round(time.time() - t_all, 2)
        meta = dict(src_meta, source=args.source_label or args.input, duration=round(dur, 2), width=info["width"], height=info["height"],
                    fps=fps, analyzed=analyzed, frames=len(kept), pruned_captions=pruned, asr=asr_name, ocr=ocr_name,
                    language=language, has_speech=bool(transcript), speech_fixes=fixed,
                    created=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        blocks = build_blocks(kept, transcript, screen_text, motions, fps, dur)
        build_pack(out, meta, kept, revisits, raw_transcript, sheets, timings, screen_text, captions, comments)
        js = json.loads((out / "pack.json").read_text(encoding="utf-8"))     # ham ve düzeltilmiş hâli birlikte sakla
        js["transcript_fused"] = [{"start": round(x["start"], 2), "end": round(x["end"], 2), "text": x["text"]} for x in transcript]
        js["timeline"] = blocks
        (out / "pack.json").write_text(json.dumps(js, ensure_ascii=False, indent=2), encoding="utf-8")
        tpath = write_transcript(out, meta, kept, blocks, sheets, comments, extra, notes, fixed)
        tk = meta["tokens"]
        log(f"bitti: {tpath}  ({timings['toplam']} sn)")
        log(f"yaklaşık token: metin {tk['text']} · kareler {tk['frames']} · kontak baskı {tk['sheets']}")
        return {"out": out, "transcript": tpath, "sheets": sheets, "meta": meta, "timings": timings}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    args = build_parser().parse_args()
    try:
        run(args)
    except VidnoteError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
